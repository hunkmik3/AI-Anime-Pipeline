# Bộ master style phim đã duyệt

Ba preset được lưu từ bộ ảnh thử Ava + hành lang ngày 2026-10-05:

| Chọn trong app | Mã lưu trên project | Hướng thể hiện |
| --- | --- | --- |
| Người thật · Hollywood | `live_action_feature` | Live-action điện ảnh, da và vật liệu tự nhiên |
| 2D Nhật hiện đại | `anime_jp_modern` | Anime vẽ tay, cel shading; chuyển động chủ yếu on twos trên timeline 24 fps |
| 2D Mỹ · Cartoon | `cartoon_us_2d` | Cartoon điện ảnh vẽ tay, diễn xuất pose-to-pose, hình khối và silhouette rõ |

## Cách dùng trong app

1. Mở board mới hoặc phần tải video gốc, chọn một trong ba style trên.
2. Dùng hồ sơ nhân vật và bối cảnh của phim hiện tại. Với nhân vật nhiều bộ đồ, chọn trạng thái cần tạo.
3. Viết prompt / gen tạo hình như bình thường. App dùng master tương ứng và Seedream qua Avis; mặc định sheet 16:9, 2K. Tỉ lệ video vẫn theo cài đặt phim.

Style được lưu trên project và dùng khi mở lại. Board đã có ảnh/video giữ nguyên style để tránh trộn tạo hình; dùng board mới cho phong cách mới. Các preset cũ và prompt đã duyệt không bị chuyển đổi hàng loạt.

## Asset để tái sử dụng

Mỗi thư mục `agent/flowboard/assets/styles/<mã-style>/` chứa:

- `character-master.txt`: master nhân vật đã dùng cho lượt thử; thay đúng `{{PROFILE}}` bằng hồ sơ bất kỳ. Bố cục 60/40: 3 góc toàn thân và 4 cận mặt, nền xám trung tính.
- `environment-master.txt`: master bối cảnh đã dùng cho lượt thử; thay `{{PROFILE}}` bằng hồ sơ bối cảnh. Một plate 16:9, không ghép nhiều khung.
- `preset.json`: nguyên văn mô tả style của chủ dự án; phần ảnh tĩnh và phần chuyển động video riêng biệt, cùng model/size/layout mặc định.

Các master không chứa hồ sơ Ava, Theo hay hành lang mẫu. Gen lần đầu mặc định chỉ dùng văn bản, không tự đính kèm ảnh ví dụ. Khi người dùng chủ động dùng ảnh nhận dạng của nhân vật/địa điểm, hoặc đạo cụ cần ảnh thành viên bên trong, ảnh đó là dữ liệu của project và được đánh số từ `@image1`; nó không thay thế style đã chọn.

Hồ sơ quyết định tuổi, ngoại hình, trang phục, thời đại, vật liệu, kiến trúc và trạng thái. Style chỉ quyết định cách thể hiện. “8K/4K detail” là mô tả chất lượng, không đổi độ phân giải xuất ảnh. Prompt video vẫn theo `CLIP_PROMPT_STANDARD.md`, giữ thoại và ngôn ngữ nguồn; style được chèn vào phần phong cách, không thay shotlist.

## Bảo trì

Runtime registry: `agent/flowboard/services/film_styles.py`. Mỗi preset có fingerprint từ config và hai master, dùng để phát hiện thay đổi ở job và production run.

Tiêu chuẩn diễn xuất/chuyển động video nằm riêng tại
`agent/flowboard/services/film_motion.py` và được truyền vào cả writer lẫn reviewer.
Người thật dùng chuyển động tự nhiên, 3D giữ khối và trọng lượng, 2D Nhật dùng
key pose và nhịp twos/holds, 2D Mỹ dùng diễn xuất theatrical và biến dạng có kiểm soát.
Tuổi, ngoại hình, phục trang, góc quay, thời gian và thoại vẫn theo dữ liệu phim.
Mốc 24 fps của anime là tham chiếu nhịp vẽ; không ép làm tròn thời gian shot nguồn.
Đổi tiêu chuẩn video chỉ đổi version tác vụ viết video, không đổi fingerprint ảnh
hay tự viết đè prompt đã duyệt. FPS thực tế phải đọc từ file xuất, không suy ra từ prompt.

Danh mục UI `frontend/src/automation/approved-film-styles.json` phản ánh nhãn và nguyên văn style; test `test_profile_film_styles.py` kiểm tra nó khớp asset backend. Khi thay mô tả style, cập nhật cả danh mục này. Khởi động lại backend sau khi thay các asset đã được cache.

Không có ảnh reference bắt buộc trong ba preset mới. Donghua trước đó vẫn giữ quy tắc ảnh mẫu riêng. Không đưa style Hollywood 3D của lượt thử vào bộ này.
