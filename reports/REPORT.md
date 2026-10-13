# Lab 25B — Report

**Họ tên / nhóm:** Vũ Văn Điền (2A202602418)

**Commit gốc:** `18cc181`

**Điểm tự động cục bộ (`scripts/grade.py --seeds 25,26,27`):** **84 / 88**. Bốn điểm E còn lại dùng bộ SLO ẩn của giảng viên; mọi phần có thể chạy cục bộ đều PASS trên cả ba seed.

Các bảng dưới đây lấy từ `reports/gameday/*.json`, `reports/workflows.json`, `reports/slo.json` và `reports/cache/*.json`, seed 25 nếu không ghi khác.

## 1. Kết quả Game Day (naive vs resilient)

| Băng | Client | availability | p95 (s) | amplification | retry_ratio | cost/turn | recovery (s) | PASS? |
|---|---|---:|---:|---:|---:|---:|---:|:---:|
| G1 | naive | 0,7194 | 7,157 | 7,738 | 6,738 | 2,163 | 4 | Không |
| G1 | resilient | 1,0000 | 4,780 | 1,063 | 0,001 | 2,104 | 0 | Có |
| G2 | naive | 0,4560 | 15,034 | 9,904 | 8,904 | 4,627 | 155 | Không |
| G2 | resilient | 1,0000 | 10,362 | 1,037 | 0,005 | 2,112 | 0 | Có |
| G3 | naive | 1,0000 | 4,768 | 1,164 | 0,164 | 2,328 | 0 | Không |
| G3 | resilient | 1,0000 | 4,621 | 1,058 | 0,030 | 2,140 | 0 | Có |
| G4 | naive | 1,0000 | 13,764 | 1,004 | 0,004 | 2,008 | 0 | Không |
| G4 | resilient | 1,0000 | 5,626 | 1,014 | 0,002 | 2,072 | 0 | Có |
| G5 | naive | 0,3699 | 5,359 | 17,030 | 16,030 | 2,124 | — | Không |
| G5 | resilient | 0,8179 | 5,477 | 1,003 | 0,002 | 2,504 | — | Có |
| G6 | naive | 0,5170 | 4,836 | 12,344 | 11,344 | 2,009 | 0 | Không |
| G6 | resilient | 1,0000 | 5,961 | 1,055 | 0,018 | 2,196 | 0 | Có |

Resilient client PASS 6/6 tape trên các seed 25, 26 và 27. Chênh lệch lớn nhất nằm ở G2: amplification giảm từ 9,904 xuống 1,037, availability tăng từ 45,60% lên 100%, và recovery giảm từ 155 giây xuống 0 giây.

## 2. Cấu hình client và lý do

| Tham số | Giá trị | Vì sao |
|---|---:|---|
| `attempt_timeout_s` | 8 s | Với 30 s, G6 chỉ còn availability 0,9488 và FAIL; 8 s đạt 1,0000. Timeout phải ngắn hơn deadline toàn turn để còn thời gian failover. |
| `max_attempts_per_provider` | 2 | Cho phép một retry cùng provider nhưng giới hạn amplification; cấu hình cuối đạt 1,037 ở G2 và 1,058 ở G3. |
| `backoff_base_s / cap_s` | 0,25 / 4 s | Full jitter đủ ngắn cho deadline 30 s nhưng phân tán retry; G1 có 0 vi phạm `retry-after`, amplification 1,063. |
| `retry_budget_ratio` | 0,10 | G3 đạt retry ratio 0,030; khi tắt budget trong thí nghiệm, tỷ lệ này tăng lên 0,158 và tape FAIL. |
| `retry_budget_min_per_s / max_tokens` | 0,5 / 20 | Vẫn cho một lượng retry nhỏ lúc traffic thấp nhưng chặn bùng nổ khi hàng nghìn call cùng lỗi. |
| `breaker.window_size / min_calls` | 20 / 10 | Đủ mẫu để tránh mở breaker vì vài lỗi nền, nhưng phản ứng đủ nhanh ở G2/G4. |
| `breaker.failure_rate_threshold` | 0,50 | Cô lập provider khi ít nhất nửa cửa sổ lỗi; G2 giữ amplification 1,037 và recovery 0 s. |
| `breaker.slow_call_s / slow_rate_threshold` | 5 s / 0,60 | Phát hiện G4 dù provider không trả error; p95 giảm từ 13,764 s của naive xuống 5,626 s. |
| `breaker.open_duration_s` | 10 s | Tránh tiếp tục dồn tải nhưng vẫn probe sớm; G2/G4/G6 đều quay lại `alpha` với primary share 1,000. |
| `breaker.half_open_max_probes / successes` | 2 / 2 | Chỉ hai probe đồng thời, cần cả hai thành công trước khi đóng breaker để tránh thundering herd. |
| `quota_cooldown_s` | 300 s | G5 chuyển khỏi family/provider hết quota mà không retry vô ích: `client_error_retries=0`, availability 0,8179. |

Sơ đồ quyết định của `on_error`:

| Loại lỗi | Ghi breaker? | Retry cùng provider? | Failover/cooldown |
|---|---|---|---|
| `CLIENT_ERROR` | Không, release permit | Không | Không; `GiveUp("client_error")` |
| `QUOTA_EXHAUSTED` | Không, release permit | Không | Cooldown 300 s, failover ngay |
| `RATE_LIMITED` | Không, release permit | Không | Cooldown đến `now + retry_after`, failover ngay; chỉ chờ nếu cooldown ≤ 5 s và không còn route |
| `OVERLOADED` | Có | Có nếu còn attempt, retry budget và deadline | Hết điều kiện thì failover |
| `SERVER_ERROR` | Có | Có nếu còn attempt, retry budget và deadline | Hết điều kiện thì failover |
| `TIMEOUT` | Có | Có nếu còn attempt, retry budget và deadline | Hết điều kiện thì failover |

Mỗi `Send` giữ một breaker permit. Success/failure có generation cũ bị bỏ qua; cancel hoặc lỗi không phản ánh provider health chỉ release permit.

## 3. Ba thí nghiệm client

### 3.1 Retry budget là lớp phòng thủ cuối

Breaker được cố ý vô hiệu bằng `min_calls=1.000.000.000`, `max_attempts_per_provider=5`. Khi giữ budget, G2 có amplification 1,378, retry ratio 0,058, availability 0,9983 và recovery 0 s; G3 có amplification 1,136, retry ratio 0,068 và PASS. Khi tắt budget bằng ratio/token/refill rất lớn, G2 tăng lên amplification 1,580, retry ratio 0,378, p95 21,482 s và recovery 24 s; G3 tăng retry ratio lên 0,158 nên FAIL. Khi breaker hoạt động đúng, cấu hình cuối còn tốt hơn (G2 amplification 1,037), nhưng vẫn cần budget vì breaker cần đủ mẫu, có thể cấu hình sai, và không giới hạn được mọi retry xảy ra trước lúc mở.

### 3.2 `attempt_timeout_s=30`

Với timeout cuối 8 s, G4/G6 đều availability 1,0000, p95 lần lượt 5,626/5,961 s. Đổi sang 30 s, G4 vẫn availability 1,0000 và p95 5,608 s vì slow-call breaker chuyển tải trước khi deadline thành vấn đề; nhưng G6 giảm availability xuống 0,9488 và FAIL vì request treo ở `alpha-eu` có thể tiêu hết deadline toàn turn. P95 G6 của các turn **thành công** còn 5,440 s nên không phản ánh số turn đã timeout; cần đọc đồng thời availability. Timeout đúng phải dựa trên latency distribution bình thường, ngân sách thời gian end-to-end và số fallback còn cần thử, không lấy nguyên deadline 30 s cho một attempt.

### 3.3 Bỏ lọc tool calling khỏi `ladder()`

Ở cấu hình đúng, G6 có `incompatible_routes=0` và availability 1,0000. Khi mọi task dùng nguyên fallback order, 257 tool call bị gửi đến `gamma-mini`, availability giảm còn 0,8922 và tape FAIL. Với product manager: hệ thống đã tìm được một model còn online nhưng model đó không biết dùng công cụ, nên 257 lượt khách nhận kết quả không thể hoàn thành tác vụ; “có response” không đồng nghĩa với “có kết quả hợp lệ”. Capability là điều kiện correctness của route, không phải tối ưu tùy chọn.

## 4. Workflow hoàn tiền

| Executor | completion | crashes | duplicate refunds | duplicate emails | lost refunds | overpaid | wasted LLM / crash |
|---|---:|---:|---:|---:|---:|---:|---:|
| naive | 1,0000 | 81 | 128 | 81 | 0 | 1.574.200 cents | 1,123 |
| ours | 1,0000 | 66 | 0 | 0 | 0 | 0 | 0,303 |

Chỉ idempotency key chưa đủ: nó de-duplicate payment/email nhưng worker mới vẫn chạy lại classify, lookup và summary, làm tăng chi phí; số checkpoint bền vững giúp resume và hạ wasted LLM xuống 0,303/crash. Chỉ checkpoint cũng chưa đủ vì worker có thể crash sau khi payment commit nhưng trước khi ghi checkpoint; retry sẽ trả tiền lần hai. Nếu ghi checkpoint **trước** payment, một lỗi trước commit hoặc crash sẽ để lại checkpoint giả, worker mới skip payment và tạo lost refund. Thứ tự đúng là execute với stable idempotency key → nhận kết quả → ghi durable checkpoint.

## 5. Alerting và error budget

| Sự cố | Mong đợi | Naive alerter | Burn-rate alerter | Độ trễ burn-rate |
|---|---|---|---|---:|
| `checkout_bug` | page | page sau 1 phút | page | 27 phút |
| `provider_outage` | page | page ngay | page | 4 phút |
| `deploy_blip` | none | **false page** | none | — |
| `slow_leak` | ticket | bỏ sót | ticket, không page | 1.337 phút |
| `degraded_evening` | page | page sau 214 phút | page | 291 phút |
| `night_spike` | none | **false page** | none | — |
| `small_bump` | none | **false page** | none | — |

Naive alerter tạo 7 page, 0 ticket và 3 false page. Burn-rate alerter tạo đúng 3 page, 1 ticket và 0 false page.

Trong 25.901.755 request, SLO 99,5% cho phép 129.508,8 bad request; thực tế có 55.618. Error budget đã tiêu **42,95%**, còn **57,05%**, availability 99,7853%, vì vậy `freeze_releases=false`. `small_bump` chỉ kéo dài 20 phút: short window có tín hiệu nhưng long window chưa đốt đủ budget ở ngưỡng page; điều kiện AND đa cửa sổ ngăn đánh thức on-call lúc 11 giờ trưa cho một blip tự hồi phục.

## 6. Postmortem — Băng G2 với client naive

![G2 naive và resilient](gameday/G2_overload_storm.png)

### Tóm tắt

Từ t=120 đến t=300, `alpha` mất 75% GPU; client naive phản ứng bằng tối đa 36 attempt/call và biến suy giảm provider thành retry storm. Trong toàn tape, 34.475 physical attempt cho 3.481 logical call tạo amplification 9,904, availability chỉ 45,60% và p95 15,034 s. Provider hồi phục ở t=300 nhưng hệ thống chỉ đạt cửa sổ 30 giây có success ≥95% từ t=455, tức chậm thêm 155 giây.

### Ảnh hưởng

- 1.285/2.362 lượt user thất bại; chỉ 1.077 lượt thành công.
- Giai đoạn từ trigger t=120 đến mốc recovery t=455 kéo dài 335 giây, trong khi sự cố GPU gốc chỉ kéo dài 180 giây.
- Có 28.381 vi phạm `retry-after`, 2.737 client timeout, 3.194 overload error và 26.298 rate-limit error.
- Nếu ngoại suy traffic quan sát 2.362 turn/600 s cho 30 ngày, monthly error budget 0,5% khoảng 51.019 failed turn; riêng sự cố dùng khoảng **2,52%** budget tháng.

### Timeline

Success ratio dưới đây là cửa sổ 10 giây kết thúc tại mốc đó; tại t=455 có thêm tỷ lệ cửa sổ 30 giây tiếp theo theo định nghĩa recovery.

| t (s) | Sự kiện | Bằng chứng từ CSV |
|---:|---|---|
| 119 | Trước sự cố, hệ thống ổn định | 9 attempts/s; success ratio 10 s = 1,000 |
| 120 | `alpha` mất 75% năng lực | 5 attempts/s tại đúng mốc; 30 s kế tiếp success ratio chỉ 0,520 |
| 143 | Retry storm đạt đỉnh | 254 attempts/s, tất cả vào `alpha`; success ratio 30 s kế tiếp = 0,000 |
| 180 | Hệ thống đã mất khả năng phục vụ | 138 attempts/s; success ratio 10 s = 0,000 |
| 300 | Provider hồi phục hoàn toàn | Vẫn 115 attempts/s; success ratio 10 s = 0,000 |
| 330 | Traffic vật lý giảm nhưng queue chưa sạch | 6 attempts/s; success ratio 10 s vẫn 0,000 |
| 400 | Vòng lặp retry/rate-limit bùng lại | 51 attempts/s; success ratio 10 s = 0,000 |
| 455 | Bắt đầu cửa sổ recovery đạt chuẩn | 13 attempts/s; trailing success chỉ 0,300 nhưng 30 s tiếp theo đạt 0,957 |
| 480 | Hệ thống ổn định trở lại | 8 attempts/s; success ratio 10 s = 1,000 |

### Trigger và root cause

**Trigger** là provider `alpha` mất 75% GPU trong 180 giây. **Root cause** là thiết kế retry ba tầng với delay cố định, không jitter, không retry budget, không circuit breaker, không failover và không tôn trọng retry-after. Mỗi timeout sinh thêm request trong khi request cũ vẫn chạy ở provider; queue đầy làm latency/timeout tăng, timeout lại sinh retry và tự duy trì vòng lặp metastable.

Sau t=300, capacity vật lý đã trở lại nhưng queue còn chứa request mà client/user đã bỏ, trong khi retry mới tiếp tục đến nhanh hơn năng lực xử lý. Vì vậy capacity hồi phục không đồng nghĩa hệ thống hồi phục; cần 155 giây để backlog và retry feedback loop tiêu tan.

### Điều gì giúp và điều gì làm tệ hơn

Việc provider trả 529/429 nhanh cho một số request giúp tránh toàn bộ request đều nằm trong queue, nhưng client lại retry cả các phản hồi này và vi phạm retry-after 28.381 lần. Timeout 8 s giới hạn thời gian một attempt chờ, song request đã timeout không bị cancel ở server nên lại làm lãng phí compute. Không có fallback khiến toàn bộ 34.475 attempt tiếp tục dồn vào `alpha` dù ba deployment khác còn khả dụng.

### Action items

| # | Hành động | Loại | Owner | Cách kiểm chứng |
|---:|---|---|---|---|
| 1 | Chuẩn hóa một tầng retry có full jitter, retry budget 10% và tôn trọng retry-after | Prevent | Agent Platform | CI chạy G1–G3 trên 3 seed: amplification ≤1,2; retry-after violations = 0; G3 retry ratio ≤0,10 |
| 2 | Triển khai sliding-window breaker có slow-call detection và HALF_OPEN probe limit | Mitigate | Reliability SDK | G2 recovery ≤30 s; G4 p95 ≤10 s; tối đa 2 probe HALF_OPEN đồng thời |
| 3 | Alert trên attempts/logical-call, retry budget saturation, breaker OPEN và queue wait | Detect | SRE/Observability | Synthetic retry storm page trong ≤5 phút khi amplification >1,6; dashboard hiển thị provider/cause |
| 4 | Dùng capability-aware fallback ladder và quota/rate-limit cooldown dùng chung | Mitigate | LLM Gateway | G5 client-error retries = 0; G6 incompatible routes = 0; primary share sau restore ≥0,8 |

## 7. Một điểm yếu còn lại

Resilient client đạt availability 100% ở G2 nhưng p95 vẫn là 10,362 s, cao hơn nhiều so với 4–6 s ở các tape bình thường. Routing hiện phản ứng sau lỗi/slow sample; trong khoảng phát hiện ban đầu, request vẫn có thể xếp hàng ở primary, và khi failover sang `beta` thì cost/call tăng từ 1,0 lên 1,4. Bước cải tiến tiếp theo là thêm concurrency limiter/load-aware routing dựa trên queue latency gần nhất, kết hợp load shedding có kiểm soát; tiêu chí là G2 p95 <8 s, amplification ≤1,2 và cost/good-turn không tăng quá 10%, thay vì hedging vô điều kiện làm tăng tải.

## 8. Cache (Part G)

| Client | availability | faq_availability | cache_hit_ratio | stale_served | wrong_answers | outdated_answers | cost/turn | PASS? |
|---|---:|---:|---:|---:|---:|---:|---:|:---:|
| resilient (không cache) | 0,8373 | 0,8424 | 0,000 | 0 | 0 | 0 | 1,386 | Không |
| cached | 0,9124 | 0,9645 | 0,652 | 157 | 0 | 3 | 0,915 | Có |

| Tham số `cache:` | Giá trị | Vì sao |
|---|---:|---|
| `similarity_threshold` | 0,90 | 0,80 tạo 157 `other_question`; 0,90 giữ wrong=0 và hit=0,652. 0,97 cũng đúng nhưng hit giảm còn 0,581 và cost tăng 0,915 → 0,964. |
| `ttl_s` | 60 s | TTL 30/60/300 cho outdated lần lượt 2/3/95; 60 giữ outdated dưới 15 nhưng hit 0,652 tốt hơn 0,592 của TTL 30. |
| `max_stale_s` | 600 s | Bằng 0 làm FAQ availability chỉ 0,8733; 600 phủ outage 120 s, phục vụ 157 stale answer và nâng FAQ availability lên 0,9645. |
| `max_entries` | 500 | Đủ cho working set C1, vẫn có LRU bound để bộ nhớ không tăng vô hạn. |

### Thí nghiệm cache

1. **Ngưỡng:** threshold 0,80 cho hit 0,666, wrong 157 (`other_question`) và cost/turn 0,965. Giá trị 0,90 cho hit 0,652, wrong 0, cost 0,915. Threshold 0,97 cho hit 0,581, wrong 0 và cost 0,964; nó an toàn nhưng bỏ lỡ nhiều paraphrase hơn. Các cặp gần mà khác như refund policy/refund time, đổi size/đổi màu và phí ship nội thành/ngoại tỉnh gây false hit ở 0,80.
2. **TTL:** TTL 30/60/300 cho hit ratio 0,592/0,652/0,731 và outdated 2/3/95. Webhook không giúp silent update phí ship ở t=480 vì client không nhận event; chỉ TTL giới hạn thời gian answer cũ còn được coi là fresh.
3. **Stale-if-error:** với `max_stale_s=0`, stale served = 0 và FAQ availability giảm từ 0,9645 xuống 0,8733. Với 600 s, 157 answer cũ nhưng còn đúng được phục vụ trong outage mà wrong vẫn bằng 0.

Câu hỏi cá nhân không được cache kể cả scope theo user vì trạng thái đơn hàng/refund/điểm thay đổi nhanh, cần kiểm tra authorization và read-after-write consistency; một TTL dùng cho FAQ không bảo đảm các thuộc tính đó. Trong outage, dashboard user-facing có thể xanh nhờ stale cache nên on-call phải alert trên direct-provider success/error/timeout, số breaker OPEN, tỷ lệ `stale_served` và tuổi cache; availability tổng hợp không được che mất provider outage.

