# Cập nhật Reskin Studio trên máy Windows

Nhánh: `claude/reskin-long-film`. Nhánh này chứa toàn bộ `codex/film-pipeline-studio-ui`
(bản máy Windows đang chạy), cộng thêm:

- trang **Reskin Studio** (`/studio`): tải video vào, chọn phong cách, độ phân giải,
  khung hình, rồi duyệt và sửa ở từng bước (shotlist, thoại, người/vật trong shot,
  material, clip, bản gen, ghép phim và xuất EDL);
- pipeline reskin nhanh hơn (chạy song song, giữ từng clip, đối chiếu nguồn theo
  từng clip, clip tới 30 s);
- lớp clay tuỳ chọn: tay chân dạng ma-nơ-canh và máy quay đo từ nguồn.

**Database không đổi** (nhánh không thêm migration). `.env`, database và media của máy
Windows được giữ nguyên. Phim đã làm trên máy Mac không đi theo Git.

## 1. Chuẩn bị

- Đợi các job đang gen xong. Sao lưu database như mọi lần cập nhật.
- `ffmpeg` và `ffprobe` phải chạy được trong PowerShell của tài khoản chạy service:
  `ffmpeg -version; ffprobe -version`.
- `.env` của máy Windows cần có (thường đã có nếu đã chạy pipeline phim):
  `AVIS_API_KEY`, `ATRIUM_CLIENT_ID`, `ATRIUM_CLIENT_SECRET` (Gemini đọc video và
  vẽ ảnh dự phòng), và bốn biến `R2_*` (reskin bắt buộc phải có R2).

## 2. Lấy code

```powershell
cd C:\flowboard
git status            # nếu có thay đổi trên máy Windows, lưu lại trước; không reset
git fetch origin
git switch --track origin/claude/reskin-long-film
```

## 3. Tắt cổng "Set up your AI provider" (nếu muốn)

Cổng này đọc biến lúc build giao diện. Tạo file `frontend\.env.local` (đã gitignore):

```powershell
Set-Content -Path frontend\.env.local -Value "VITE_DISABLE_SETUP_GATE=1"
```

## 4. Cài phụ thuộc và build

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy.ps1
```

Rồi cài thêm phần cho reskin vào venv của backend:

```powershell
cd agent
.\.venv\Scripts\pip.exe install -e ".[pose]"
```

Runtime tách nhạc (Demucs) chạy trong một venv riêng. Bỏ qua nếu máy đã có
`agent\.venv-audio`. Cần `uv` (`winget install astral-sh.uv`):

```powershell
uv venv --python 3.11 .venv-audio
uv pip install --python .venv-audio\Scripts\python.exe "demucs==4.0.1" "torch==2.6.0" "torchaudio==2.6.0" "soundfile>=0.12"
cd ..
```

Lần đầu dùng, máy sẽ tự tải mô hình về cache: Depth Anything + YuNet (Hugging Face),
RTMPose/YOLOX (khoảng 150 MB, chỉ khi dùng lớp khung xương), Whisper và htdemucs.
Máy cần mạng ở lần chạy đầu.

## 5. Khởi động lại backend

PowerShell (Administrator):

```powershell
Restart-Service giantstudio-agent
Get-Service giantstudio-agent
```

Service phải chạy uvicorn **không có** `--reload` hay `--workers` (như
`install-services.ps1` đã cài). Hai cờ đó làm Windows đổi kiểu event loop, và khi đó
không gọi được ffmpeg hay worker clay.

## 6. Kiểm tra

1. Mở `https://giantstudio.reelmind.co/api/automation/reskin/capabilities` (đã đăng nhập).
   Mọi mục trong `checks` phải là `true`. Mục nào `false` thì xem lại bước tương ứng
   (`ffmpeg`, `motion_runtime`, `audio_runtime`, `avis`, `atrium`, `reference_storage`).
2. Mở `https://giantstudio.reelmind.co/studio`, tải lại trang (Ctrl+F5). Tab
   **Reskin Studio** nằm trên thanh trên cùng.
3. Chạy thử một phim ngắn trước: ở form Phim mới đặt **Chỉ làm N phút đầu = 1**. Theo dõi
   tới khi có clip đầu tiên rồi mới chạy phim dài.

Các kiểm tra tự động của nhánh chạy trên macOS/PostgreSQL. Runtime Windows chưa được
kiểm thử trực tiếp, nên bước 6.3 là bước bắt buộc.

## Quay lại bản cũ

```powershell
git switch codex/film-pipeline-studio-ui
powershell -ExecutionPolicy Bypass -File .\deploy.ps1
Restart-Service giantstudio-agent
```

Không cần rollback database, vì nhánh không đổi schema. Thiết lập studio được lưu trong
các trường JSON có sẵn, và bản cũ bỏ qua chúng.
