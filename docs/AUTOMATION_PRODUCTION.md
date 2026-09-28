# Automation production — giai đoạn 1 và 2

Triển khai ngày 2026-09-28. Các thay đổi dùng chung cho mọi phim; không gắn với tên nhân vật hoặc dự án mẫu. Giữ nguyên các nhánh Avis, KYC/người thật và B2B hiện có. Không có bước AI tự kiểm tra video đầu ra.

## Phạm vi đã triển khai

### 1. Công việc chạy trên server

- Board đã lưu gửi job viết prompt, tạo material/keyframe và tạo video vào bảng `automation_job`. Các tác vụ nguồn (phân tích, chuyển thể, cast, thiết kế, đối chiếu/refine/conform) cũng được lưu trước khi chạy, kể cả khi chưa có board.
- Worker khởi động cùng backend. Đóng tab không hủy job. Lease 90 giây, heartbeat 20 giây; sau restart worker nhận lại công việc hết lease. Các bước nguồn tận dụng checkpoint sẵn có; một số lời gọi phân tích đang dở có thể phải chạy lại.
- Một request key luôn chỉ tới cùng một payload; các click đồng thời cùng đầu vào/đích dùng lại job đang chạy. Không tự gửi lại yêu cầu gen khi chưa biết provider đã nhận hay chưa.
- Có receipt video thì tiếp tục poll đúng mã đó. Mất kết nối giữa lúc gửi và nhận receipt thì chuyển `unknown` để đối soát. Đây không phải bảo đảm exactly-once từ provider; ảnh chưa có receipt có thể cần đối soát thủ công.
- Autosave gửi revision và tuần tự hóa các lần lưu. Tab cũ nhận 409, giữ bản sửa cục bộ thay vì ghi đè. Xuất JSON trước khi tải lại để giữ bản sửa. Kết quả job được ghép từ server, không dựa vào tab phải mở.
- Prompt hoàn tất được giữ trong lịch sử job; không tự thay bản người dùng đã sửa hoặc prompt của shotlist đã thay đổi.

### 2. Shot nguồn và continuity

- Nhập board mặc định giữ từng shot nguồn, cả cut dưới một giây. UID, mốc nguồn, duration và thứ tự được giữ; các khoảng bị thiếu/chồng lấn trong phân đoạn truyện được chuẩn hóa để không mất hoặc lặp shot.
- Writer dùng mốc thời gian đến millisecond; không kéo dài shot để ép thoại. Phần đệm do giới hạn thời lượng provider được ghi là silent hold nằm ngoài timeline dựng. Shot/clip vượt giới hạn provider cần phân đoạn hợp lệ; không âm thầm gộp shot.
- Production manifest lưu phiên bản theo nội dung của nhân vật, quần chúng, bối cảnh, đạo cụ, reference và từng shot. Snapshot được lưu khi nội dung sản xuất thay đổi, không chỉ vì kéo node trên canvas.
- Trạng thái đầu/cuối từng shot kế thừa người và vật trong cùng scene; `offscreen` không đồng nghĩa với rời cảnh. Đổi scene tạo phạm vi continuity mới, kể cả quay lại một nơi sau scene khác. Scene chưa rõ được tách phạm vi và ghi chú, không đoán từ tên bối cảnh.
- `continuity_events` ghi nhận exit hoặc lý do đổi holder/hand/state/wardrobe/contains_ids. Thay đổi thiếu lý do được hiển thị để xem lại. Thông tin không quan sát được giữ là last-known, không biến thành bằng chứng nguồn.
- Writer nhận production context và phiên bản tài sản; kiểm tra lại shotlist/context trước khi chạy. Thoại giữ nguyên ngôn ngữ kịch bản đã khóa.

## Cách dùng

1. Mở board đã lưu, mở **Sản xuất** ngay dưới thanh đầu trang.
2. Để bật **Giữ từng shot nguồn khi nhập board** cho phim theo hướng shot-by-shot. Board cũ đã gộp shot cần nhập lại từ phân tích nguồn; thay checkbox không tự khôi phục dữ liệu đã mất. Nên nhập vào board mới để giữ bản cũ.
3. Bấm **Kiểm tra trạng thái cảnh và phiên bản tài sản**. Xem trạng thái đầu/cuối, ghi lý do chuyển tiếp dựa trên nguồn. Dropdown phiên bản cho phép xem các snapshot cũ; không tự rollback.
4. Viết prompt/gen bằng nút sẵn có; job xuất hiện trong mục Sản xuất. Có thể đóng tab rồi mở lại.
5. Với `Cần đối soát`, lấy đúng mã từ Avis và bấm tiếp tục theo dõi. Nếu provider thực sự chưa nhận request, kiểm tra trực tiếp rồi ghi lý do và xác nhận; lần gen kế tiếp là yêu cầu mới có thể tính phí. Không dùng xác nhận này chỉ vì đang chờ lâu.
6. Prompt bị giữ lại do bản sửa mới có thể xem ở **Xem prompt đã lưu** trong job tương ứng.

## Vận hành

Áp dụng migration `b728a91d6c03` sau khi sao lưu PostgreSQL, trước khi khởi động backend mới. Frontend mới dùng API jobs; endpoint đồng bộ cũ vẫn tồn tại để tương thích bản nháp chưa lưu/script cũ. Muốn được bảo vệ bởi queue, script phải chuyển sang API jobs.

`FLOWBOARD_AUTOMATION_CONCURRENCY=8` là số job đồng thời **mỗi process backend**, giới hạn 1–64. Đây không phải số request LLM bên trong tác vụ nguồn: source vẫn có cấu hình concurrency riêng. Nhiều worker/process làm tăng tổng tải. Restart để thay cấu hình. Poll video có deadline và giới hạn lỗi; lỗi kéo dài giữ receipt để đối soát.

API chính (phân quyền theo project như các route automation hiện có):

- `POST/GET /api/automation/projects/{id}/jobs`
- `GET /api/automation/projects/{id}/jobs/{job_id}`
- `POST .../jobs/{job_id}/cancel` — chỉ trước khi worker nhận
- `POST .../jobs/{job_id}/resume` — poll receipt cũ, không gen lại
- `POST .../jobs/{job_id}/resolve-absent` — xác nhận thủ công chưa gửi
- `GET .../production?sequence_key=...`
- `GET .../revisions` và `GET .../revisions/{revision}`

Chỉ hủy được job chưa dispatch. Không tự xóa project còn job đang chạy hoặc chưa đối soát. Sao lưu database cùng media để giữ được snapshot lẫn file; manifest chỉ chứa định nghĩa và đường dẫn reference, không sao chép byte ảnh.

## Giới hạn và kiểm thử

Đây là nền tảng điều phối và kiểm soát dữ liệu trước gen, chưa phải toàn bộ hệ thống mass production: chưa có scheduler theo ngân sách/SLA, tự dựng timeline thành phim, hoặc bảo đảm chất lượng của video sinh ra. Chưa đo benchmark 15–60 phút phim để cam kết xử lý trong 10–30 phút.

Continuity phụ thuộc scene/asset/presence từ phân tích nguồn hoặc dữ liệu người dùng duyệt. Ledger không thay thế việc đọc video. Chi tiết chưa rõ không được tự bịa; lỗi/ghi chú nguồn vẫn cần xem xét. Prompt chính xác làm giảm lỗi, không bảo đảm model giữ cut/đạo cụ/thoại tuyệt đối.

Các bài test dùng PostgreSQL `flowboard_test` và provider giả lập: kiểm tra đồng thời, dedupe, lease, receipt trước poll, không submit lại, timeout mơ hồ, autosave conflict, nguồn phục hồi, snapshot, continuity, cut ngắn và prompt stale. Không gọi gen ảnh/video trả phí để kiểm thử triển khai này.

## Giai đoạn 3 — chuẩn bị từng shot

Mở **Sản xuất → Chuẩn bị từng shot → Chuẩn bị / cập nhật 5 clip đầu**. Phần biên dịch material là xử lý cục bộ. Khi chưa có kế hoạch raccord phù hợp, luồng chuẩn bị tự gọi Luna qua Avis; bước đó có chi phí text theo provider, nhưng chưa phát sinh lượt gen ảnh/video. Mỗi clip nhận `sequence.shot_package` gồm:

- Material/reference cụ thể, phiên bản, trang phục đang chọn; phụ thuộc của quần chúng và đạo cụ.
- Bố cục, camera, ánh sáng, hành động, thoại tiếng Anh và trạng thái continuity cho mỗi shot.
- Anchor bối cảnh theo phạm vi cảnh: giữ geometry/nguồn sáng, không sao chép screen-left/right khi đảo góc.
- Gợi ý shot cần keyframe vì mở cảnh, quần chúng, đạo cụ hoặc chuyển trạng thái.
- Thiếu ảnh, nhiều bộ trang phục chưa có binding riêng, dữ liệu chuyển thể lỗi, cut từng bị gộp và thoại CJK được nêu rõ. Phát hiện CJK chỉ là kiểm tra nhanh, không phải bộ nhận diện mọi ngôn ngữ. Reviewer prompt vẫn kiểm tra câu thoại theo kịch bản.

Khi đã chuẩn bị, nút viết prompt cập nhật package, lấy thêm reference cho tài sản kế thừa và kiểm tra binding thực tế. Writer và reviewer độc lập của luồng strict đều nhận package. Worker kiểm tra package vẫn đúng trước khi gọi model; thay đổi shot/material buộc chuẩn bị lại. Target adaptation đã duyệt được áp dụng trên bản sao, không ghi đè bằng chứng nguồn.

Trong từng shot có nút **Xem prompt đầu/cuối shot** và **Gen ảnh đầu/cuối shot**. Prompt ảnh giữ style từ phần STYLE của sheet đã chốt, bố cục và thứ tự reference. Nút gen mới phát sinh lượt ảnh trả phí, dùng model đang chọn trên board, qua durable queue. Kết quả ở `video.shotFrames`, kèm phiên bản package. Đây là keyframe xem trước cho từng shot; chưa tự thay bộ reference video hoặc tự ghép các frame thành phim. Không thay đổi đường KYC/B2B hiện tại. Khi dùng giao diện, bước lập raccord chạy tự động trước writer và keyframe.

API thêm:

- `GET .../shot-packages?limit=5` hoặc `?sequence_key=...`
- `GET .../shot-keyframe?sequence_key=...&shot_index=0&which=start` — preview miễn phí
- `POST .../shot-keyframe` — queue ảnh, yêu cầu `sequence_key`, `shot_index`, `which`, `expected_revision`, `request_key`

Package cũ không tự cập nhật theo thời gian; bấm cập nhật hoặc viết prompt để lấy bản mới. Trạng thái “Đủ material” không phải đã duyệt video hay đã xác minh mọi ghi chú nguồn. Board cũ chưa chuẩn bị vẫn dùng luồng cũ. Bộ điều phối giai đoạn 4 được mô tả bên dưới.

## Raccord toàn cảnh — chạy tự động trước gen

Bước viết prompt (`writer`) của board đã lưu giờ tự gọi planner raccord, chờ job trên server hoàn tất, lấy package hiện tại và viết tiếp. Keyframe cũng tự chuẩn bị raccord trước khi gen. Không cần duyệt từng shot, không có approval flag cho kế hoạch raccord. Nút chuẩn bị trong mục Sản xuất là lối xem trước; không phải điều kiện phải bấm thủ công để writer chạy.

- Planner mặc định **gpt-6-luna qua Avis**, cấu hình `FLOWBOARD_RACCORD_MODEL`. Gom toàn bộ các shot trong cùng phạm vi cảnh, kể cả qua nhiều clip. Những cảnh độc lập chạy song song trong durable queue hiện có.
- Kế hoạch ghi hướng dàn cảnh, cách giữ quần chúng/đạo cụ/trang phục, xử lý chi tiết chưa rõ và quan hệ phụ thuộc giữa các shot. Trường `predecessor_shot_id` là chỉ dẫn kế hoạch; bộ điều phối giai đoạn 4 bên dưới thực thi quan hệ này qua các clip bằng frame nối.
- Kế hoạch là production intent, không ghi đè shot, thoại, thời lượng, bằng chứng nguồn hay trạng thái duyệt nguồn. Writer/reviewer nhận kế hoạch qua shot package; các sự kiện/quan hệ đã xác minh vẫn có ưu tiên cao hơn.
- Cache theo project, phạm vi cảnh, nội dung shot/material, model và phiên bản planner. Kéo node hoặc chạy lại với cùng đầu vào không gọi model lại. Đổi dữ liệu làm kế hoạch cũ không còn được gắn vào package.
- Request gửi model dùng bảng chung cho các trạng thái lặp lại, không lặp nguyên dữ liệu mỗi shot. Dữ liệu gốc vẫn ở payload job. Chỉ shot có kết luận chuyển tiếp thiếu căn cứ được thay bằng quy tắc thận trọng; các shot hợp lệ còn lại được giữ, với nhãn `ai_with_rules`. Model được thử tối đa hai vòng lập/sửa cấu trúc; mỗi vòng có deadline mặc định 120 giây (`FLOWBOARD_RACCORD_TIMEOUT_S`). Hết hạn/lỗi thì dùng quy tắc thận trọng, ghi rõ `rules_fallback`; không giả là AI đã giải quyết mọi mâu thuẫn và không yêu cầu người dùng duyệt để tiếp tục.
- `continuity_break: true` trên shot tạo phạm vi mới dù cùng địa điểm. Ranh giới scene do dữ liệu nguồn cung cấp; planner không tự biến suy đoán thành một sự kiện nguồn. Scene không xác định tiếp tục được tách riêng để tránh kéo nhầm người/đạo cụ qua cảnh khác.
- Các lỗi đầu vào thực sự như reference thiếu, adaptation hỏng, cut gộp trái chế độ giữ nguồn vẫn là lỗi dữ liệu. Raccord tự động không âm thầm bỏ các kiểm tra đó, cũng không bỏ xác minh nguồn hoặc KYC.

API: `POST /api/automation/projects/{id}/raccord` với `expected_revision`, tùy chọn `sequence_key`; trả các durable job của toàn cảnh liên quan. Bỏ `sequence_key` để lập cho cả board. Kết quả và trạng thái ở API jobs. Không có bước kiểm tra video đầu ra, chọn take tự động hay gen lại để sửa lỗi.

## Giai đoạn 4 — lượt sản xuất trên server (2026-09-28)

Phần này bổ sung bộ điều phối còn thiếu ở các giai đoạn trên. Vào **Sản xuất → Chạy sản xuất toàn board**. Điểm bắt đầu là **board đã nhập shotlist, hồ sơ và dữ liệu đối chiếu nguồn**; thao tác upload/phân tích/chuyển thể/đưa video nguồn lên board vẫn dùng luồng nguồn hiện có. Bộ điều phối không tự phê duyệt bằng chứng còn tranh chấp, không sửa mất cut nguồn và không ghi đè board bằng một bản phân tích mới.

- **Kiểm tra kế hoạch** chỉ đọc dữ liệu, không gọi model. **Bắt đầu** mới cấp lượt chạy theo cấu hình: material + prompt, hoặc material + prompt + video + bản dựng. Không thêm bước duyệt từng shot.
- Material được tạo theo đồ thị phụ thuộc: identity trước sheet trang phục, thành viên trước nhóm quần chúng, thành phần trước đạo cụ chứa chúng. Giữ prompt đã chốt khi có, dùng cấu trúc prompt hiện tại khi thiếu. Chu trình phụ thuộc chưa giải được, node thiếu, cut bị gộp, thời lượng sai hoặc nguồn chưa đủ xác minh được báo trước khi chạy. Khi vượt chín slot reference, hệ thống thử ghép atlas cơ học theo giới hạn bên dưới; vượt khả năng đóng gói mới bị chặn.
- Material hiện có được ghi nhận cùng phiên bản đầu vào. Cache job theo nội dung, trong phạm vi project. Khi thay thiết kế, lượt mới tính lại material/prompt và các clip liên quan; clip/cảnh không bị ảnh hưởng dùng lại job phù hợp. Một lượt đang chạy dừng xếp việc nếu các định nghĩa sản xuất thay đổi. Việc kéo node không làm mất cache.
- Sau material, Luna/Avis lập raccord toàn cảnh. Writer các clip độc lập chạy song song, dùng trạng thái nguồn và kế hoạch cảnh thay cho việc chờ câu văn `end_state` của writer trước.
- Quan hệ `predecessor_shot_id` qua hai clip giờ được thực thi: chờ clip trước, trích ảnh tại thời điểm cuối shot đó (theo timeline nguồn, trước phần đệm), gắn ảnh làm **reference continuity** vào hợp đồng prompt của clip sau. Vẫn gửi đủ reference identity/wardrobe/đạo cụ theo thứ tự và KYC/người thật của board. Đây là ảnh hướng dẫn, **không phải cam kết khóa pixel đầu/cuối của chế độ i2v**. Source facts và sheet đã chốt có ưu tiên cao hơn lỗi trong ảnh sinh ra. Phụ thuộc giữa các shot bên trong một clip vẫn được diễn đạt trong prompt đa shot.
- Tùy chọn ảnh đầu/cuối tạo thêm hai ảnh hướng dẫn mỗi clip, cũng đi vào bộ reference. Mặc định tắt để tránh thêm lượt ảnh và đầy chín slot. Không thay chế độ provider đang hoạt động. KYC thiếu media ID được ingest lại từ ảnh cũ, không gen lại ảnh.
- Hạn mức là **số job mới**, không phải giá tiền: mặc định 100 ảnh, 100 video, 500 job text (mỗi writer/planner có thể gọi model nhiều lần theo cơ chế kiểm tra hiện có). Cache không tính thêm lượt mới. Có giới hạn song song riêng ảnh/video/text cho mỗi project. Tổng đang thực thi vẫn chịu `FLOWBOARD_AUTOMATION_CONCURRENCY` của worker; tăng con số trên UI không tự vượt giới hạn server. Hết hạn mức dừng xếp việc và báo rõ.
- Bộ điều phối không giữ slot worker trong lúc chờ. Trạng thái và các job con lưu trong DB hiện có, không cần migration mới. Restart/đóng tab vẫn tiếp tục. **Dừng xếp thêm việc** không hủy job đã xếp/gửi. Job `unknown` dừng nhánh; không tự submit lại. Job thất bại cũng không tự gen lại để sửa lỗi. Có thể đối soát hoặc chạy lại riêng tác vụ rồi tiếp tục; thay đầu vào/hạn mức thì bắt đầu lượt mới để dùng lại kết quả phù hợp.
- Bản dựng dùng FFmpeg: thứ tự nguồn, cắt phần đệm sau editorial duration, chuẩn hóa kích thước/fps/audio, giữ audio clip, bổ sung track im lặng cho clip không có audio. Mặc định 24 fps. Clip ngắn hơn thời lượng dựng được báo lỗi, không kéo giãn hoặc bù cảnh. Không tự đo lại vị trí cut mà model thực tế tạo ra. Đường tải phim kiểm tra project và lưu file ở `storage/production-renders/`.
- Không có AI kiểm tra video đầu ra, chọn take, chỉnh thoại, grading hay gen lại sửa lỗi. Ghép phim là bản dựng đầu tiên từ các take được tạo, không phải chứng nhận chất lượng studio.

API:

- `POST .../production-runs/preview` — cấu hình, kiểm tra không phát sinh gen.
- `POST .../production-runs` — `expected_revision`, `request_key`, `config`; mặc định `mode: prepare`. `sequence_keys: []` nghĩa là toàn board.
- `POST .../production-runs/{run_id}/pause` hoặc `/resume`.
- `GET .../production-runs/{run_id}/film` — file MP4 của lượt hoàn tất.
- Trạng thái lượt và tác vụ vẫn ở API `/jobs`, kind `production_run`, `result.stage/tasks/created_counts/output`.

Kiểm thử gồm provider giả lập cho cache, cap, recovery, thứ tự phụ thuộc, KYC và frame handoff; FFmpeg thật với clip tổng hợp ngắn cho padding/audio/fps. Chưa đo SLA 10–30 phút trên phim 15–60 phút và chưa chạy một lượt gen trả phí toàn phim bằng bộ điều phối này. Board 0925 cũ còn cut gộp sẽ bị preflight chặn đúng như trước; cần nhập lại từ nguồn thay vì bỏ kiểm tra.

### Atlas tự động và quan hệ material (bổ sung)

Lượt sản xuất tự giảm số slot reference khi cần. Ưu tiên giữ ảnh nhân vật riêng, đóng các ảnh còn lại thành trang tối đa bốn ảnh gốc, 1536 px mỗi ô (contain, không crop), có nhãn `CELL 1…4`. Đây là ghép ảnh cơ học từ reference đã có, không gọi model vẽ lại. Atlas có media ID để dùng cùng đường KYC hiện có. Ảnh góc đầu/cuối và frame nối clip được tính vào hạn mức chín slot trước khi đóng gói.

Mỗi tài sản vẫn có binding riêng với URL nguồn, ô atlas, URL atlas và media ID. Package giữ reference gốc; contract chỉ thay lớp vận chuyển. Server kiểm tra receipt của job atlas đúng project, đúng ảnh nguồn và đúng ô trước writer/gen. Không dùng cờ client để bỏ kiểm tra. Atlas được cache theo nguồn và bố cục. Atlas không bảo đảm model sẽ đọc đúng mọi chi tiết; ưu tiên ít ô và không ép vô hạn tài sản vào một ảnh. Quá khả năng bốn ô/trang thì vẫn yêu cầu phân đoạn clip.

`generation_depends_on_asset_ids` là thứ tự tạo ảnh được khai báo rõ. `member_ids` vẫn là phụ thuộc thật. Với chu trình chỉ gồm `depends_on_asset_ids` cũ, khi tất cả thành viên đã có reference, bộ điều phối giữ quan hệ đó làm thông tin liên quan và tái sử dụng ảnh, không bắt gen theo vòng tròn. Nếu còn ảnh thiếu hoặc có cạnh generation/member rõ ràng trong chu trình, hệ thống vẫn báo cần thứ tự hợp lệ. Không đoán quan hệ từ tên tài sản. Sau khi một nhóm dùng chung reference đã được ghi nhận, đổi thiết kế trong nhóm yêu cầu khai báo thứ tự tạo mới, tránh vòng gen lại vô hạn.

Kiểm tra board 0925: các chặn do vòng chai/chất lỏng và giới hạn reference được giải quyết; tám lỗi cut gộp vẫn được giữ để sửa từ nguồn. Lượt thử provider thật dùng một project bản sao, chỉ chọn CLIP 05–06, tối đa hai video mới, không gen thêm material.

Lượt thử thật còn phát hiện hai lỗi ở writer: (1) coi ước lượng tốc độ nói mặc định là bằng chứng thoại nguồn không vừa shot, và (2) lặp tag `@imageN` ngoài dòng khai báo. Policy mới giữ nguyên lời thoại/mốc shot, cho phép chỉ dẫn nhịp nói bám nguồn và coi ước lượng là advisory. Bộ chuẩn hóa chỉ đổi lần nhắc tag dư thành `reference image N` khi có đúng một khai báo hợp lệ; khai báo trùng/sai và tag lạ vẫn bị chặn. Kiểm tra coverage và reviewer độc lập vẫn chạy sau bước chuẩn hóa.
