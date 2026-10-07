# Cập nhật pipeline phim trên Windows

Nhánh: `codex/film-pipeline-studio-ui`.

Bản này gồm pipeline video nguồn → shotlist → tạo hình chính → material →
prompt → video, các style đã chốt, Chat Agent, khung clip kéo thả và thanh tiến độ
gọn. Cần cập nhật cả backend, database và frontend.

## Máy Windows đã có repo

Thực hiện trong thư mục repo bằng PowerShell. Nếu `git status` có thay đổi trên
máy Windows, lưu chúng trước khi chuyển nhánh; không dùng reset để bỏ thay đổi.

```powershell
git status
git fetch origin
git switch --track origin/codex/film-pipeline-studio-ui
```

Nếu đã có nhánh local này, dùng `git switch codex/film-pipeline-studio-ui` rồi
`git pull --ff-only`.

Giữ nguyên `.env`, database và thư mục media của máy Windows. Nhánh Git chỉ
chứa code và asset style đi kèm; không chứa API key, tài khoản, database hay các
phim đã gen trên máy Mac. Muốn chuyển các project cũ phải chuyển dữ liệu riêng.

## Cài phụ thuộc, migrate và build

Cần Python 3.10+ (khuyến nghị 3.12), Node.js/npm, PostgreSQL và `ffmpeg`/`ffprobe`
trong PATH của tài khoản chạy backend. Với thiết lập Docker có sẵn của repo,
mở Docker Desktop trước. Pipeline này dùng PostgreSQL; không dùng chế độ
`-NoDocker`/SQLite trong hướng dẫn cũ.

Đợi các job đang gen hoàn tất trước khi bảo trì. Sao lưu database, dừng backend
service đang dùng, rồi chạy từ thư mục repo:

```powershell
ffmpeg -version
ffprobe -version
powershell -ExecutionPolicy Bypass -File .\deploy.ps1
```

Script cài phụ thuộc backend, chạy `alembic upgrade head` và build frontend.
Migration mới `c83a7d9e0412` thêm bảng hội thoại Chat Agent. Script giữ `.env` đã
có; đối chiếu `.env.example` để bổ sung các biến model tùy chọn khi cần. Nếu đây
là máy mới chưa có `.env`, script tạo file mẫu và yêu cầu điền cấu hình trước khi
chạy tiếp. Với PostgreSQL ngoài Docker, giữ cấu hình database hiện có và chạy
các bước pip, Alembic, npm của script trên môi trường đó.

Script không tự khởi động lại backend. Nếu máy dùng service do
`install-services.ps1` tạo, mở PowerShell Administrator và chạy:

```powershell
Start-Service giantstudio-agent
Get-Service giantstudio-agent
```

Nếu dùng tên service hoặc cách chạy khác, khởi động lại đúng backend đó. Không
cần cài lại service hay tunnel cho lần cập nhật này. Mở lại app, tải mới trang và
kiểm tra Canvas, Tạo hình chính, Chat Agent và Tiến độ trước khi gửi lượt gen.

Các kiểm tra tự động của nhánh chạy trên macOS/PostgreSQL; chưa kiểm thử trực
tiếp runtime Windows.
