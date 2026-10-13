# Hướng dẫn hoàn thành Lab 25B — Reliability cho Production Agent

Tài liệu này được viết sau khi rà soát toàn bộ mã nguồn, cấu hình, test, simulator, dữ liệu SLI, rubric và report template trong repository. Mục tiêu là giải thích bài lab như một hệ thống hoàn chỉnh và chia công việc thành các checkpoint có thể kiểm chứng độc lập.

> Trạng thái starter repo khi khảo sát: các phần A, B, C, D, E và G còn `TODO`/`NotImplementedError`; chưa có `reports/REPORT.md` hay kết quả chấm. Trên máy được khảo sát, cả lệnh `python` và `py` đều chưa có trong `PATH`, vì vậy cần hoàn thành Checkpoint 0 trước khi chạy test.

> Trạng thái sau triển khai: cả 12 checkpoint đã hoàn thành; 82/82 unit test PASS, 6/6 Game Day tape PASS trên seed 25/26/27, workflow và public SLO PASS, cache tape C1 PASS trên cả ba seed. Report, scoreboard và biểu đồ postmortem đã được tạo trong `reports/`.

## 1. Bài lab yêu cầu làm gì?

Bạn đóng vai on-call của **ShopAgent**, một agent CSKH xử lý yêu cầu hoàn tiền. Mỗi lượt thông thường có hai LLM call:

1. `tool`: model quyết định gọi công cụ, bắt buộc provider hỗ trợ tool calling.
2. `chat`: model viết câu trả lời cho khách hàng.

Phiên bản ban đầu `NaiveClient` retry ở ba tầng (app × gateway × SDK), có thể tạo tối đa 36 attempt cho một logical call. Khi provider quá tải, những retry này tiếp tục chiếm tài nguyên dù client đã timeout, làm hàng đợi không thoát được ngay cả sau khi provider hồi phục. Đây là **retry storm** dẫn đến **metastable failure**.

Mục tiêu của bài lab là thay client này bằng một lớp reliability có:

- phân loại lỗi và quyết định retry/failover đúng;
- exponential backoff với full jitter và retry budget;
- sliding-window circuit breaker có CLOSED/OPEN/HALF_OPEN;
- fallback theo năng lực model và deadline;
- semantic cache an toàn, có TTL, invalidation và stale-if-error;
- workflow có side effect đạt exactly-once effect;
- multi-window burn-rate alerting và error budget;
- report có số liệu và postmortem blameless.

Tổng điểm là 100: **88 điểm tự động** và **12 điểm report chấm tay**.

| Phần | Nội dung | Điểm |
|---|---|---:|
| A | Phân loại lỗi, jitter, retry budget | 12 |
| B | Circuit breaker | 12 |
| C | ResilientClient + 6 incident tapes | 30 |
| D | Workflow exactly-once | 12 |
| E | Burn-rate alerting + error budget | 12 |
| G | Semantic cache + cache tape | 10 |
| F | Report và postmortem | 12 |

## 2. Bản đồ dự án

```text
configs/
  gameday.yaml             Cấu hình workload, provider và các tham số được phép tune
  scenarios.yaml           6 incident tapes G1–G6; không sửa
  scenarios_cache.yaml     Cache tape C1; không sửa
data/
  sli_30d.csv              43.200 phút dữ liệu total/bad request
  sli_30d_labels.json      Nhãn sự cố và kỳ vọng page/ticket/none
docs/
  RUBRIC.md                Cách chấm chi tiết
reports/
  REPORT_TEMPLATE.md       Mẫu phải copy thành REPORT.md
scripts/
  run_gameday.py           Chạy incident tapes và xuất JSON/CSV
  run_workflows.py         Chạy 500 workflow có fault injection
  run_slo.py               So sánh naive alert với burn-rate alert
  plot_timeseries.py       Vẽ biểu đồ G2 cho postmortem
  grade.py                 Autograder 88 điểm
src/gameday_lab/
  errors.py                Part A1 — phân loại lỗi
  retry.py                 Part A2 — jitter và retry budget
  breaker.py               Part B — circuit breaker
  client.py                Part C — ghép A + B thành resilient client
  cache.py                 Part G — semantic cache và caching client
  naive_client.py          Baseline để so sánh; không sửa
  factory.py               Tạo client/executor/alerter theo tên
  sim/                     Discrete-event simulator; không sửa
  workflows/executor.py    Part D — bài làm workflow
  workflows/services.py    Các API giả lập lỗi; không sửa
  workflows/runner.py      Fault runner; không sửa
  slo/alerting.py          Part E — bài làm alerting
  slo/data.py              Sinh/đọc dữ liệu; không sửa
  slo/scoring.py           Chấm alert; không sửa
tests/                     Hợp đồng hành vi công khai; không sửa
```

### Các file được phép sửa

- `src/gameday_lab/errors.py`
- `src/gameday_lab/retry.py`
- `src/gameday_lab/breaker.py`
- `src/gameday_lab/client.py`
- `src/gameday_lab/cache.py`
- `src/gameday_lab/workflows/executor.py`
- `src/gameday_lab/slo/alerting.py`
- Chỉ hai khối `client:` và `cache:` trong `configs/gameday.yaml`
- File mới và test bổ sung của bạn
- `reports/REPORT.md`

### Các file không được sửa

Không sửa `src/gameday_lab/sim/`, các baseline `naive_*`, `workflows/services.py`, `workflows/runner.py`, `slo/data.py`, `slo/scoring.py`, các file scenario và toàn bộ `tests/`. Grader dùng bản riêng của các file này; thay đổi chúng không giúp bài chạy trên grader và có thể bị trừ toàn bộ phần liên quan.

## 3. Kiến trúc và luồng chạy

### 3.1 Luồng Game Day

```text
gameday.yaml + scenarios.yaml
              │
              ▼
         load_config()
              │
              ▼
factory.build_client() ──► NaiveClient / ResilientClient / CachingClient
              │
              ▼
      discrete-event simulator ──► ProviderSim (queue, rate limit, fault)
              │
              ├─ start(call, now) ──────────────► Send / GiveUp / Serve
              ├─ on_success(attempt, ...) ──────► cập nhật breaker/cache
              ├─ on_error(attempt, error, ...) ─► retry / failover / give up
              └─ on_cancel(...) ────────────────► giải phóng breaker permit
              │
              ▼
 metrics + checks + JSON/CSV trong reports/
```

Thời gian trong simulator là **thời gian ảo**. Mọi quyết định phải dựa vào tham số `now` và `call.deadline`; tuyệt đối không dùng `time.time()` hay `time.sleep()`.

Một provider là một hàng đợi FIFO có số server hữu hạn. Nếu client timeout, request không bị hủy ở provider và vẫn tiêu tốn compute. Vì vậy retry quá nhanh làm tăng `amplification`, tăng chi phí và kéo dài thời gian hồi phục.

### 3.2 Hợp đồng giữa client và simulator

- `CallContext`: logical call, task, deadline, state dùng riêng cho call, và request/answer cho FAQ.
- `Send`: gửi một physical attempt đến provider, có timeout, delay và `tag` tùy ý.
- `GiveUp`: dừng logical call và trả degraded answer.
- `Serve`: trả một `Answer` đã từng được simulator sinh ra từ cache.
- `Attempt`: kết quả của một lần gửi, mang lại chính `tag` đã gắn trong `Send`.
- `ProviderError`: có `status`, `code` và có thể có `retry_after`.

`Permit` của circuit breaker được đặt vào `Send.tag`. Nhờ vậy kết quả trả về biết nó thuộc generation nào và có phải HALF_OPEN probe hay không.

### 3.3 Ba nhánh độc lập của bài lab

- A → B → C → G là nhánh client/cache; Part G dùng `ResilientClient` khi chạy tape C1.
- D là workflow hoàn tiền, có thể làm độc lập sau khi setup.
- E là SLO/alerting, cũng có thể làm độc lập.
- F cần số liệu từ C, D, E và G nên làm cuối.

## 4. Các provider và incident tape

| Provider | Family | Tool calling | Cost/call | Đặc điểm |
|---|---|:---:|---:|---|
| `alpha` | A-large | Có | 1.0 | Primary, 18 server, tenant limit 20 rps |
| `alpha-eu` | A-large | Có | 1.0 | Cùng model khác region, 16 server, 10 rps |
| `beta` | B-large | Có | 1.4 | 30 server, đắt hơn |
| `gamma-mini` | C-mini | Không | 0.2 | Nhanh/rẻ nhưng không dùng được cho `tool` |

| Tape | Sự cố | Điều kiện PASS |
|---|---|---|
| G1 | Traffic ×2,5; `alpha` bị giới hạn 12 rps | availability ≥ 0,98; 0 vi phạm retry-after; amplification ≤ 1,6 |
| G2 | `alpha` mất 75% capacity trong 180 giây | availability ≥ 0,95; amplification ≤ 1,6; recovery ≤ 30 s; primary share sau hồi phục ≥ 0,8 |
| G3 | `alpha` trả 500 cho 30% request | availability ≥ 0,98; retry ratio ≤ 0,10; amplification ≤ 1,6 |
| G4 | `alpha` chậm thêm 5 giây nhưng không lỗi | availability ≥ 0,97; p95 ≤ 10 s; quay lại primary ≥ 0,8 |
| G5 | 60% request malformed, sau đó family A hết quota | 0 retry sau lỗi 400; unneeded failover ≤ 10; availability ≥ 0,78 |
| G6 | `alpha` 503, `alpha-eu` treo, `beta` hạ rate limit | availability ≥ 0,95; 0 tool call vào model không hỗ trợ tool |
| C1 | FAQ + update tài liệu + outage toàn bộ provider | wrong = 0; outdated ≤ 15; FAQ availability ≥ 0,93; hit ratio ≥ 0,50 |

Grader ẩn thay seed, thời điểm và cường độ sự cố. Không hard-code tên tape, timestamp hay đọc state nội bộ simulator.

## 5. Danh sách checkpoint

| # | Checkpoint | Kết quả đầu ra | Kiểm chứng chính |
|---:|---|---|---|
| 0 | Chuẩn bị môi trường và baseline | Python/venv/dependency sẵn sàng | import được package, baseline chạy |
| 1 | Part A1 — phân loại lỗi | 4 hàm trong `errors.py` | `test_errors_retry.py` phần classification |
| 2 | Part A2 — backoff và budget | `FullJitterBackoff`, `RetryBudget` | toàn bộ `test_errors_retry.py` pass |
| 3 | Part B — circuit breaker | State machine đầy đủ | `test_breaker.py` pass |
| 4 | Part C — ResilientClient logic | routing/retry/failover/deadline | `test_client.py` pass |
| 5 | Part C — Game Day tuning | 6/6 tapes PASS | `run_gameday.py --client both` |
| 6 | Part G1 — SemanticCache | lookup/store/invalidate/LRU | test cache thuần pass |
| 7 | Part G2 — CachingClient và C1 | stale-if-error + config phù hợp | toàn bộ `test_cache.py` và C1 PASS |
| 8 | Part D — workflow exactly-once | idempotency + checkpoint + resume | `test_executor.py`, 500 workflow PASS |
| 9 | Part E — burn-rate alerting | alert episodes + budget report | `test_alerting.py`, public SLI PASS |
| 10 | Part F — report/postmortem | `reports/REPORT.md` đầy số liệu | đối chiếu rubric 12 điểm |
| 11 | Kiểm tra và đóng gói | scoreboard + artifact đầy đủ | `grade.py`, lint, Git diff |

Thứ tự khuyến nghị là 0 → 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 11. Nếu chia việc, D và E có thể thực hiện song song với nhánh A–C–G.

## 6. Hướng dẫn chi tiết từng checkpoint

### Checkpoint 0 — Chuẩn bị môi trường và ghi baseline

**Mục tiêu:** có Python ≥ 3.10, môi trường độc lập và biết starter đang thất bại ở đâu.

Trong PowerShell:

```powershell
python --version
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev,plot]"
```

Nếu PowerShell chặn activate script, chỉ nới policy cho process hiện tại rồi activate lại:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\.venv\Scripts\Activate.ps1
```

Nếu máy có `py` thay vì `python`, dùng `py -3.11` để tạo venv. Nếu cả hai lệnh không tồn tại, cài Python 3.10+ và chọn tùy chọn thêm Python vào `PATH`.

Chạy baseline:

```powershell
python -m pytest -q
python scripts/run_gameday.py --client naive
python scripts/run_workflows.py --executor naive
python scripts/run_slo.py --alerter naive
```

Kết quả starter dự kiến: simulator sanity pass, còn các test gọi hàm `TODO` fail; naive G2 có amplification lớn hơn 5 và workflow naive tạo duplicate refund.

**Definition of done**

- [ ] `python --version` là 3.10 trở lên.
- [ ] `python -c "import yaml, pytest, gameday_lab"` không lỗi.
- [ ] Đã lưu output baseline để so sánh ở report.
- [ ] Không sửa file cấm chỉ để làm test pass.

---

### Checkpoint 1 — Part A1: Phân loại lỗi

**File:** `src/gameday_lab/errors.py`

**Mục tiêu:** biến lỗi SDK/provider thành một `ErrorKind`, rồi tách ba câu hỏi khác nhau: có retry cùng provider không, có ghi lỗi vào breaker không, và failover có ích không.

Thứ tự phân loại nên rõ ràng:

```text
status 0 hoặc 408                  -> TIMEOUT
status 429 + insufficient_quota    -> QUOTA_EXHAUSTED
status 429 còn lại                 -> RATE_LIMITED
status 503 hoặc 529                -> OVERLOADED
status 5xx còn lại                 -> SERVER_ERROR
còn lại                            -> CLIENT_ERROR
```

Sau đó triển khai:

- `is_retryable`: true cho rate limit, overload, server error và timeout; false cho quota/client error.
- `counts_toward_breaker`: chỉ true khi lỗi phản ánh sức khỏe provider: overload, server error, timeout.
- `failover_helps`: false duy nhất với client error; request malformed sẽ sai ở mọi provider.

Điểm quan trọng là **429 rate limit khác 429 hết quota**. Rate limit có thể hết sau `retry_after`; quota cần cooldown dài và chuyển provider/family.

Chạy test tập trung:

```powershell
python -m pytest tests/test_errors_retry.py -q -k "classify or retryable or breaker_relevant or failover"
```

**Definition of done**

- [ ] 11 case classification trong test đúng.
- [ ] Lỗi 400 không retry, không failover, không mở breaker.
- [ ] Rate limit không làm breaker đánh giá provider hỏng.
- [ ] Không dùng `except Exception` làm thay đổi semantics phân loại.

---

### Checkpoint 2 — Part A2: Full jitter và retry budget

**File:** `src/gameday_lab/retry.py`

#### Bước 1: `FullJitterBackoff.delay`

Với retry thứ `n >= 1`:

```text
ceiling = min(cap_s, base_s * 2 ** (n - 1))
delay   = rng.uniform(0, ceiling)
```

Nếu server gửi `retry_after`, không dùng công thức trên mà trả:

```text
retry_after + rng.uniform(0, base_s)
```

Luôn dùng `self.rng` đã được seed để simulator lặp lại được. `retry_number < 1` phải raise `ValueError`.

#### Bước 2: `RetryBudget`

Đây là token bucket dùng chung cho client:

- bucket bắt đầu đầy `max_tokens`;
- mỗi logical request mới nạp `ratio` token;
- thời gian trôi nạp thêm `elapsed * min_per_s`;
- tổng token luôn bị cap bởi `max_tokens`;
- một retry cần ít nhất 1 token và tiêu đúng 1 token.

Có thể tạo private helper `_refill(now)` để cả hai public method dùng chung:

```text
nếu chưa có _last: đặt _last = now
ngược lại:
    elapsed = max(0, now - _last)
    tokens = min(max_tokens, tokens + elapsed * min_per_s)
    _last = now
```

`record_request(now)` gọi refill rồi cộng `ratio`. `try_acquire_retry(now)` gọi refill, kiểm tra `tokens >= 1`, trừ token nếu cho phép.

Chạy:

```powershell
python -m pytest tests/test_errors_retry.py -q
```

**Definition of done**

- [ ] Jitter trải trên toàn khoảng và không vượt cap.
- [ ] Không retry sớm hơn `retry_after`.
- [ ] 1.000 request đồng thời với ratio 0,1 chỉ tạo khoảng 100 retry cộng token ban đầu.
- [ ] Bucket refill theo `now` ảo và không vượt `max_tokens`.

---

### Checkpoint 3 — Part B: Sliding-window circuit breaker

**File:** `src/gameday_lab/breaker.py`

**Mục tiêu:** hoàn thiện state machine và bảo vệ HALF_OPEN trước concurrent probes.

#### Bước 1: Ghi kết quả ở CLOSED

Trong `_record`:

1. Append `(failed, slow)` vào `_window`.
2. Chỉ giữ `window_size` phần tử gần nhất.
3. Chưa đủ `min_calls` thì không trip.
4. Tính `failure_rate = số failed / số phần tử`.
5. Tính `slow_rate = số (slow and not failed) / số phần tử`.
6. Ưu tiên trip vì failure rate; nếu không thì xét slow rate.
7. Mọi transition phải gọi `_move`, không tự gán `state`.

#### Bước 2: Cấp permit

- CLOSED: trả permit thường với generation hiện tại.
- OPEN trước khi hết `open_duration_s`: trả `None` để fail fast.
- OPEN đã hết timeout: `_move(HALF_OPEN, "open_timeout_elapsed")` rồi xử lý như HALF_OPEN.
- HALF_OPEN: chỉ cấp tối đa `half_open_max_probes`; mỗi permit probe làm tăng `probes_in_flight`.

#### Bước 3: Xử lý kết quả

Luôn kiểm tra `permit.generation == self.generation` trước. Kết quả cũ sau một transition phải bị bỏ qua.

- Success ở CLOSED: ghi slow nếu `latency_s >= slow_call_s`.
- Success nhanh ở HALF_OPEN: giảm in-flight, tăng success count; đủ ngưỡng thì CLOSED.
- Success chậm ở HALF_OPEN: mở lại với `probe_slow`.
- Failure ở CLOSED: `_record(failed=True)`.
- Failure ở HALF_OPEN: mở lại với `probe_failed`.
- `release`: chỉ giải phóng slot probe, không ghi health result.

Chạy:

```powershell
python -m pytest tests/test_breaker.py -q
```

**Các invariant nên tự kiểm tra**

- `probes_in_flight` không âm và không vượt giới hạn.
- `_move` tăng generation, reset probe counter và log transition.
- Breaker không trip trước `min_calls`.
- Window bỏ kết quả cũ, không dùng toàn bộ lịch sử.

**Definition of done**

- [ ] 12 test breaker pass.
- [ ] Transition path đi được CLOSED → OPEN → HALF_OPEN → CLOSED.
- [ ] 50 request đồng thời ở HALF_OPEN chỉ nhận tối đa số permit cấu hình.
- [ ] Slow success có thể trip breaker.

---

### Checkpoint 4 — Part C: Ghép thành `ResilientClient`

**File:** `src/gameday_lab/client.py`

**Mục tiêu:** kết hợp classification, cooldown, retry budget, backoff, breaker, deadline và fallback ladder.

#### Bước 1: Định nghĩa invariant cho `call.state`

Nên dùng thống nhất:

```python
{
    "ladder": [...],  # provider hợp lệ cho task
    "idx": 0,         # provider hiện tại
    "tries": 0,       # số Send đã phát cho provider hiện tại
}
```

Mỗi khi chuyển provider: `idx += 1`, đặt `tries = 0`, và gửi failover ngay (`delay = 0`). Mỗi khi `_route` phát một `Send`, tăng `tries` đúng một lần.

#### Bước 2: `ladder(task)`

- Task `tool`: lọc bỏ provider có `supports_tools=False`.
- Task `chat`: giữ nguyên `fallback_order`.

Không hard-code riêng `gamma-mini`; lọc theo metadata để qua hidden provider config.

#### Bước 3: `start`

1. Gọi `budget.record_request(now)` đúng một lần cho logical call.
2. Khởi tạo state.
3. Gọi `_route` để chọn attempt đầu tiên.

#### Bước 4: `_route`

Duyệt ladder từ `idx` hiện tại:

1. Nếu `now + delay < cooldown_until[provider]`, skip provider.
2. Nếu không cooldown, gọi helper `_send` được cung cấp.
3. `_send` tự xin breaker permit, cap timeout theo deadline và có thể trả `None` khi breaker từ chối.
4. Khi skip/failover, tăng `idx`, reset `tries` và reset delay về 0.
5. Nếu hết ladder, chỉ được chờ cooldown ngắn nhất khi:
   - thời gian chờ ≤ `RATE_LIMIT_WAIT_MAX_S`;
   - breaker tương ứng không OPEN;
   - delay + thời gian attempt tối thiểu vẫn nằm trong deadline.
6. Nếu không có lựa chọn hợp lệ, trả `GiveUp("no_provider_available")` hoặc `GiveUp("deadline")` theo nguyên nhân.

#### Bước 5: `on_error`

Áp dụng đúng bảng quyết định:

| ErrorKind | Breaker | Hành động |
|---|---|---|
| CLIENT_ERROR | `release` permit | GiveUp ngay, không retry/failover |
| QUOTA_EXHAUSTED | `release` | cooldown dài `quota_cooldown_s`, failover ngay |
| RATE_LIMITED | `release` | cooldown đến `now + retry_after`, failover ngay |
| OVERLOADED/SERVER_ERROR/TIMEOUT | `on_failure` | retry cùng provider nếu còn lượt, budget cho phép và deadline đủ; nếu không thì failover |

Với transient error, thứ tự an toàn là:

1. Báo failure cho breaker của provider vừa dùng.
2. Nếu còn quota attempt trên provider hiện tại, xin retry token.
3. Nếu có token, tính backoff từ số retry và gọi `_route` với delay.
4. Nếu breaker vừa OPEN, `_route` sẽ không gửi lại provider đó và chuyển tiếp.
5. Nếu không retry được, tăng `idx`, reset `tries`, failover ngay.

#### Bước 6: Success và cancel

- `on_success`: gọi đúng breaker bằng `attempt.provider` và chuyển `attempt.tag`/latency vào `breaker.on_success`.
- `on_cancel`: delayed attempt chưa được gửi nhưng đã giữ permit; gọi `release` để không khóa vĩnh viễn HALF_OPEN slot.

Chạy:

```powershell
python -m pytest tests/test_client.py -q
python -m pytest tests/test_errors_retry.py tests/test_breaker.py tests/test_client.py -q
```

**Definition of done**

- [ ] Tool ladder không chứa provider thiếu tool calling.
- [ ] Rate-limit một call làm các call khác cùng tránh provider đến hết cooldown.
- [ ] Client error không retry và không mở breaker.
- [ ] Transient error retry cùng provider tối đa theo config rồi failover.
- [ ] Mọi `delay + timeout` nằm trong deadline.
- [ ] Cancel giải phóng HALF_OPEN permit.

---

### Checkpoint 5 — Part C: Chạy và tune 6 Game Day tapes

**File tune:** chỉ khối `client:` trong `configs/gameday.yaml`.

Chạy từng tape trước để vòng lặp nhanh:

```powershell
python scripts/run_gameday.py --client both --scenario G1_rate_limit_spike
python scripts/run_gameday.py --client both --scenario G2_overload_storm
python scripts/run_gameday.py --client both --scenario G3_brownout
python scripts/run_gameday.py --client both --scenario G4_slow_region
python scripts/run_gameday.py --client both --scenario G5_bad_deploy_then_quota
python scripts/run_gameday.py --client both --scenario G6_provider_outage
```

Sau đó chạy toàn bộ và thêm seed để tránh overfit:

```powershell
python scripts/run_gameday.py --client resilient
python scripts/run_gameday.py --client resilient --seed 26
python scripts/run_gameday.py --client resilient --seed 27
```

Đọc cả output console, `reports/gameday/resilient.json` và CSV timeseries.

#### Cách chẩn đoán theo metric

| Metric xấu | Khả năng nguyên nhân | Hướng điều chỉnh |
|---|---|---|
| amplification cao | retry quá nhiều/breaker mở chậm | giảm attempt, siết budget, giảm min_calls hợp lý |
| retry_ratio > 0,10 ở G3 | budget quá rộng | giảm ratio/token khởi tạo hoặc breaker phản ứng sớm hơn |
| p95 cao ở G4/G6 | timeout/slow threshold quá dài | giảm attempt timeout hoặc slow-call threshold |
| availability thấp | timeout quá ngắn, breaker quá nhạy, ladder hết sớm | cân bằng timeout/probe/retry/failover |
| recovery > 30 s | retry storm còn giữ queue hoặc open duration dài | giảm amplification/open duration, kiểm tra retry budget |
| primary share < 0,8 | cooldown/open duration quá dài, breaker không close | kiểm tra HALF_OPEN và transition timing |
| retry-after violation > 0 | quay lại rate-limited provider quá sớm | kiểm tra cooldown dùng `now + retry_after` |
| incompatible route > 0 | ladder không lọc capability | sửa `ladder`, không tune để che lỗi |
| client-error retry > 0 | 400 vẫn bị retry/failover | sửa decision table |

Không tune chỉ cho seed 25. Sau mỗi thay đổi, ghi lại trước/sau vì report yêu cầu lý do gắn với số liệu.

**Definition of done**

- [ ] 6/6 public tapes PASS với seed mặc định.
- [ ] 6/6 vẫn ổn trên ít nhất hai seed bổ sung.
- [ ] Không hard-code scenario/timestamp.
- [ ] Có bảng log tham số → metric tác động để dùng cho report.

---

### Checkpoint 6 — Part G1: `SemanticCache`

**File:** `src/gameday_lab/cache.py`

**Mục tiêu:** cache FAQ theo semantic similarity nhưng không trộn scope, user hay nội dung đã hết hạn.

#### `lookup(req, now)`

1. Câu hỏi `personal=True` luôn miss.
2. Loại entry có `age > ttl_s + max_stale_s`.
3. Chỉ xét entry có cùng `prompt_version`.
4. Tính cosine với mọi candidate và chọn similarity lớn nhất.
5. Nếu best score dưới threshold thì miss.
6. Nếu hit, cập nhật `last_used=now` và trả `CacheHit` với:
   - `age_s = now - stored_at`;
   - `fresh = age_s <= ttl_s`.

#### `store(req, answer, now)`

1. Không lưu câu hỏi personal.
2. Trong cùng scope, nếu đã có entry similarity ≥ threshold thì replace entry đó để refresh.
3. Nếu chưa có thì append entry mới.
4. Nếu vượt `max_entries`, evict entry có `last_used` nhỏ nhất.

#### `invalidate(source, now)`

Xóa mọi entry có `source` trong `answer.sources` và trả số entry đã xóa. Tham số `now` thuộc interface nhưng không cần thay đổi tiêu chí invalidation.

Chạy test lớp cache trước:

```powershell
python -m pytest tests/test_cache.py -q -k "same_question or paraphrase or picks_the_most or personal_questions or scope_by_prompt or ttl_then or invalidate_by_source or store_refreshes or bounded_size"
python -m pytest tests/test_cache.py -q
```

**Definition of done**

- [ ] Paraphrase trên threshold hit; near-miss dưới threshold miss.
- [ ] Chọn entry giống nhất, không phải entry đầu tiên.
- [ ] Personal request không bao giờ store/lookup hit.
- [ ] Prompt v1 không phục vụ prompt v2.
- [ ] Fresh → stale → expired đúng biên thời gian.
- [ ] LRU và invalidation theo source đúng.

---

### Checkpoint 7 — Part G2: `CachingClient`, stale-if-error và tape C1

**File:** `src/gameday_lab/cache.py`; **file tune:** khối `cache:` trong `configs/gameday.yaml`.

#### Hoàn thiện wrapper

- `start`:
  - không có `call.request` thì bypass cache;
  - fresh hit thì `Serve(answer, stale=False)` ngay;
  - miss hoặc stale hit thì thử inner client trước;
  - nếu inner trả `GiveUp`, `_or_stale` có thể cứu bằng cache.
- `on_success`:
  - báo success cho inner client trước để breaker nhận kết quả;
  - nếu call có request và `call.answer`, store answer.
- `on_error`: lấy action từ inner client rồi đưa qua `_or_stale`.
- `on_content_update`: invalidate mọi answer dựa trên source vừa đổi.
- `_or_stale`: chỉ thay `GiveUp`; lookup lại tại thời điểm hiện tại; nếu có hit thì `Serve`, đánh dấu stale theo `hit.fresh`.

Lookup lại trong `_or_stale` là bắt buộc vì webhook có thể invalidate entry trong lúc request đang thử provider.

#### Tune C1

Starter cố ý đặt threshold 0,80, TTL 600 và stale window 0 — ba giá trị này lần lượt dễ gây false hit, outdated answer và mất availability khi outage.

Chạy:

```powershell
python -m pytest tests/test_cache.py -q
python scripts/run_gameday.py --scenarios configs/scenarios_cache.yaml --client resilient,cached --out reports/cache
```

Quy trình tune:

1. Thử threshold 0,80 / giá trị ứng viên / 0,97; theo dõi `wrong_answers_by_reason`, hit ratio và cost.
2. Thử TTL 30 / 60 / 300; theo dõi `outdated_answers` sau silent update ở t=480.
3. Thử `max_stale_s=0` rồi tăng đủ phủ outage t=240–360; theo dõi `faq_availability` và `stale_served`.
4. Dùng ngưỡng cao hơn các near-miss khoảng 0,80–0,86 nhưng vẫn thấp hơn paraphrase khoảng 0,94–0,98. Giá trị cuối phải được chứng minh bằng tape, không chỉ suy đoán.
5. Giữ personal traffic ngoài cache kể cả nếu nghĩ rằng có thể scope theo user: dữ liệu đơn hàng thay đổi nhanh và yêu cầu consistency/authorization mạnh hơn FAQ.

**Definition of done**

- [ ] `tests/test_cache.py` pass.
- [ ] C1 có `wrong_answers = 0`.
- [ ] `outdated_answers <= 15`.
- [ ] `faq_availability >= 0.93` và `cache_hit_ratio >= 0.50`.
- [ ] Đã lưu số liệu ba thí nghiệm cache cho mục 8 của report.

---

### Checkpoint 8 — Part D: Workflow exactly-once effect

**File:** `src/gameday_lab/workflows/executor.py`

Workflow gồm:

```text
classify_request → lookup_order → issue_refund → send_email → write_summary
```

Service có ba kiểu lỗi:

- `ServiceError`: lỗi trước khi thực thi, retry an toàn.
- `ServiceTimeout`: có thể đã commit nhưng mất response.
- `WorkerCrash`: process chết sau commit; không được catch, runner sẽ tạo worker mới.

#### Bước 1: Stable keys

`idempotency_key(req, step)` phải deterministic theo ít nhất `session_id + step`; không dùng UUID/random/time. Nên có namespace rõ để không trùng với checkpoint key.

`checkpoint_key(req, step)` cũng deterministic nhưng thuộc namespace checkpoint. Hai session khác nhau hoặc hai step khác nhau phải có key khác nhau.

#### Bước 2: Retry đúng exception

`_retry` thử tối đa `max_attempts`, chỉ catch `ServiceError` và `ServiceTimeout`, rồi re-raise exception cuối. Không catch `WorkerCrash`; lớp này kế thừa `BaseException` chính để tránh bị nuốt bởi `except Exception`.

#### Bước 3: Execute rồi checkpoint

Với từng step trong `STEPS`:

1. Tính checkpoint key.
2. Nếu store đã có kết quả, load vào `state[step]` và skip execution.
3. Nếu chưa có, gọi `_step(req, state, step)`.
4. Chỉ sau khi step trả thành công mới `store.put(key, result)`.
5. Đặt `state[step] = result` để step sau dùng.
6. Cuối cùng trả `{"status": "done", **state}`.

Vì sao cần cả hai cơ chế:

- Chỉ checkpoint: crash xảy ra sau payment commit nhưng trước checkpoint sẽ chạy payment lần nữa.
- Chỉ idempotency key: không trả tiền/email trùng nhưng mọi read/LLM step bị tính lại sau restart, làm tăng chi phí và `wasted_llm_per_crash`.
- Ghi checkpoint trước khi execute: crash/exception có thể để lại checkpoint giả và làm mất refund.

Chạy:

```powershell
python -m pytest tests/test_executor.py -q
python scripts/run_workflows.py --executor both
```

**Definition of done**

- [ ] 0 duplicate refund.
- [ ] 0 duplicate email.
- [ ] 0 lost refund và 0 overpaid.
- [ ] Completion ≥ 99%.
- [ ] Wasted LLM/crash ≤ 0,6.
- [ ] WorkerCrash thoát ra để runner restart.

---

### Checkpoint 9 — Part E: Burn-rate alerting và error budget

**File:** `src/gameday_lab/slo/alerting.py`

SLO là 99,5%, nên error budget ratio là `1 - 0.995 = 0.005`. Burn rate của một cửa sổ:

```text
(bad_requests / total_requests) / (1 - SLO)
```

#### Bước 1: Prefix sums

Với values `[v0, v1, ...]`, tạo:

```text
prefix[0] = 0
prefix[i + 1] = prefix[i] + values[i]
```

Tổng cửa sổ kết thúc tại `end`, dài `length`:

```text
start = max(0, end - length + 1)
sum = prefix[end + 1] - prefix[start]
```

Nhờ đó 43.200 phút × nhiều cửa sổ được xử lý O(n), không phải cộng lại mỗi cửa sổ.

#### Bước 2: Burn rate

Tính tổng total/bad trên cùng cửa sổ; nếu total bằng 0 thì trả 0.0.

#### Bước 3: Tạo alert episodes

Với từng rule, scan từng phút. Rule firing khi **cả long window và short window** đều đạt threshold. Khi trạng thái chuyển false → true, ghi `start`; khi true → false, tạo một `AlertEvent(start, end_exclusive)`. Nếu còn firing ở cuối dữ liệu, đóng episode tại `len(total)`. Cuối cùng sort toàn bộ event theo `start_min`.

Default rules:

| Rule | Severity | Long + short | Burn rate |
|---|---|---|---:|
| Fast burn | page | 60 phút + 5 phút | 14,4× |
| Slow burn | page | 360 phút + 30 phút | 6× |
| Budget leak | ticket | 3 ngày + 6 giờ | 1× |

Long window đảm bảo sự cố đủ đáng kể; short window đảm bảo sự cố vẫn đang xảy ra và giúp alert reset nhanh sau recovery.

#### Bước 4: Error budget report

Tính đúng các field/rounding trong docstring:

- `requests = sum(total)`
- `allowed_bad = (1-slo) * requests`
- `bad = sum(bad)`
- `availability = 1 - bad/requests`
- `budget_consumed = bad/allowed_bad`
- `budget_remaining = 1 - budget_consumed`
- `freeze_releases = budget_remaining <= 0`

Thêm guard hợp lý cho dataset không có request để tránh chia 0.

Chạy:

```powershell
python -m pytest tests/test_alerting.py -q
python scripts/run_slo.py --alerter both
```

Kiểm tra public labels: checkout bug/provider outage/degraded evening phải page; slow leak phải ticket nhưng không page; deploy blip/night spike/small bump không được gây false page.

**Definition of done**

- [ ] Unit tests alerting pass.
- [ ] Mọi incident public được phát hiện đúng deadline.
- [ ] `false_pages = 0`.
- [ ] Error budget report có quyết định freeze đúng.

---

### Checkpoint 10 — Part F: Report và postmortem

Tạo file làm bài:

```powershell
Copy-Item reports/REPORT_TEMPLATE.md reports/REPORT.md
```

Không viết nhận định chung chung. Mọi kết luận quan trọng phải gắn với số trong:

- `reports/gameday/naive.json` và `resilient.json`;
- các CSV timeseries;
- `reports/cache/*.json`;
- `reports/workflows.json`;
- `reports/slo.json`;
- `reports/scoreboard.json`.

#### Thu số liệu chuẩn

```powershell
python scripts/run_gameday.py --client both
python scripts/run_workflows.py --executor both
python scripts/run_slo.py --alerter both
python scripts/run_gameday.py --scenarios configs/scenarios_cache.yaml --client resilient,cached --out reports/cache
python scripts/plot_timeseries.py --scenario G2_overload_storm
```

#### Ba thí nghiệm client bắt buộc

1. Làm breaker gần như vô hiệu (`min_calls` cực lớn), cho phép nhiều attempt; so sánh bật/tắt retry budget trên G2/G3.
2. Đặt `attempt_timeout_s=30`, chạy G4/G6, ghi tác động lên p95 và availability.
3. Tạm bỏ capability filter khỏi `ladder`, chạy G6, giải thích `incompatible_routes` bằng ngôn ngữ sản phẩm.

Mỗi thí nghiệm nên dùng bản config tạm hoặc ghi lại giá trị cuối để restore. Không để cấu hình thí nghiệm trở thành cấu hình nộp bài.

#### Ba thí nghiệm cache bắt buộc

1. Threshold 0,80 / giá trị cuối / 0,97.
2. TTL 30 / 60 / 300.
3. `max_stale_s=0` so với giá trị cuối.

#### Postmortem G2

Postmortem phải:

- có ít nhất 5 mốc timeline lấy từ CSV;
- phân biệt trigger (mất 75% GPU) và root cause (retry ba tầng, không jitter/budget/breaker);
- giải thích vì sao sau t=300 provider hồi phục mà queue vẫn đầy request client đã bỏ và retry tiếp tục giữ tải cao;
- nêu impact bằng số user thất bại, thời lượng và error budget;
- giữ giọng blameless, tập trung vào thiết kế/quy trình;
- có ít nhất 3 action item, phân loại prevent/detect/mitigate, có owner và tiêu chí đo được.

Ví dụ tiêu chí kiểm chứng tốt: “CI chạy G2 trên 3 seed và yêu cầu amplification ≤ 1,2, recovery ≤ 30 s”.

**Definition of done**

- [ ] Đủ bảng 6 tapes × 2 clients.
- [ ] Mỗi tham số client/cache có lý do dựa trên số liệu.
- [ ] Đủ 3 thí nghiệm client và 3 thí nghiệm cache.
- [ ] Workflow, alerting và error budget có câu trả lời cơ chế.
- [ ] Postmortem đạt toàn bộ rubric 5 điểm.
- [ ] Mục “một điểm yếu còn lại” có tình huống cụ thể và hướng cải tiến.

---

### Checkpoint 11 — Kiểm tra cuối và đóng gói

Chạy từ môi trường sạch nếu có thể:

```powershell
python -m pip install -e ".[dev]"
python -m pytest -q
python scripts/grade.py
python -m ruff check src tests scripts
git status --short
git diff --check
```

`scripts/grade.py` ghi `reports/scoreboard.json`. Public autograder chỉ có một seed mặc định; trước khi nộp nên chủ động chạy thêm seed cho Game Day/cache/workflow vì giảng viên chấm bằng ba seed và scenario/SLI ẩn.

Checklist nộp bài:

- [ ] Code không còn `NotImplementedError("TODO(student)...")` trong file được giao.
- [ ] Tất cả unit tests pass.
- [ ] 6/6 Game Day tapes pass.
- [ ] C1 cache tape pass.
- [ ] Workflow và public SLO pass.
- [ ] `reports/REPORT.md` hoàn chỉnh.
- [ ] `reports/scoreboard.json` mới nhất.
- [ ] Hình `reports/gameday/G2_overload_storm.png` tồn tại nếu report tham chiếu.
- [ ] Không sửa file cấm.
- [ ] Không hard-code tape, timestamp, seed hoặc document name trong logic.
- [ ] Repo chạy được sau `pip install -e ".[dev]"` trên máy sạch.

## 7. Chiến lược debug nhanh

Khi một test fail, sửa theo tầng thấp nhất trước:

```text
errors/retry ─► breaker ─► client ─► game day ─► cache tape
```

- Nếu `test_client` fail, đừng tune YAML trước; kiểm tra invariant `idx/tries`, permit và cooldown.
- Nếu unit test pass nhưng tape fail, đọc metric và timeseries rồi mới tune config.
- Nếu C1 có wrong answer, đọc `wrong_answers_by_reason`:
  - `other_question`: threshold quá thấp;
  - `other_user`/`personal_cached`: cache personal data;
  - `old_prompt`: thiếu prompt-version scope;
  - `ignored_invalidation`: webhook không xóa entry.
- Nếu C1 chỉ có `outdated_answers`, đó là silent update; webhook không thể giúp, phải giảm TTL.
- Nếu FAQ availability thấp trong outage, stale window quá ngắn hoặc stale không được dùng khi inner client `GiveUp`.
- Nếu workflow trùng side effect, kiểm tra idempotency key có ổn định qua retry/restart không.
- Nếu workflow mất side effect, kiểm tra có ghi checkpoint trước execution không.
- Nếu alert false page trên blip, kiểm tra điều kiện AND giữa long và short window, cùng ranh giới episode.

## 8. Những nguyên tắc cần nhớ sau bài lab

1. Retry là tải bổ sung; nó phải có classification, deadline, backoff, jitter và budget.
2. Circuit breaker và retry budget giải quyết hai lớp khác nhau: breaker cô lập endpoint xấu, budget giới hạn thiệt hại ngay cả khi breaker cấu hình sai/chưa đủ mẫu.
3. Fallback chỉ hợp lệ khi route tương thích với yêu cầu sản phẩm.
4. Exactly-once effect không đến từ retry; nó cần stable idempotency key và durable checkpoint.
5. Cache availability không được đánh đổi bằng dữ liệu sai hoặc rò rỉ dữ liệu cá nhân.
6. Alert tốt đo tốc độ đốt error budget trên nhiều cửa sổ, không chỉ nhìn error rate tức thời.
7. Một thay đổi reliability chỉ được coi là tốt khi có số liệu về availability, latency, amplification, recovery, cost và correctness.

