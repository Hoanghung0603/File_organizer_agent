# File Organizer Agent với Human-in-the-loop

Project Python minh họa một agent dùng OpenRouter để tự khảo sát cây thư mục, lựa chọn công cụ cần thiết và đề xuất kế hoạch tổ chức. Agent chỉ được sử dụng các công cụ đọc trong giai đoạn khảo sát. Việc tạo thư mục, di chuyển hoặc đổi tên tệp chỉ diễn ra sau khi người dùng phê duyệt kế hoạch.

## Workflow

```text
Goal
  ↓
Agent chọn công cụ chỉ đọc
  ↓
Observation
  ↓
Agent tiếp tục khảo sát hoặc gửi kế hoạch
  ↓
Python kiểm tra kế hoạch
  ↓
Human review
  ↓
Executor xác định
  ↓
Verification và audit log
```

Agent có năm công cụ khảo sát:

| Công cụ | Chức năng |
|---|---|
| `list_files` | Liệt kê đệ quy các tệp trong thư mục |
| `inspect_metadata` | Đọc phần mở rộng, loại nội dung, kích thước và thời gian sửa đổi |
| `read_file` | Đọc bản xem trước có giới hạn từ TXT, Markdown, CSV, DOCX và PDF |
| `inspect_image` | Phân tích ảnh hoặc trang đầu của PDF quét |
| `find_duplicates` | Phát hiện tệp trùng chính xác bằng kích thước và SHA-256 |

`submit_plan` kết thúc giai đoạn khảo sát và gửi kế hoạch cho Python kiểm tra. Nó không thay đổi filesystem.

## Cấu trúc chính

```text
main.py
requirements.txt
.env.example
sandbox/
logs/
src/file_organizer/
├── cli.py
├── file_tools.py
├── llm.py
└── models.py
tests/
└── test_organizer.py
```

- `llm.py` chứa goal, tool schema, vòng lặp agent và bộ kiểm tra kế hoạch.
- `file_tools.py` chứa công cụ đọc, kiểm tra đường dẫn và executor.
- `cli.py` hiển thị trace, kế hoạch, bước phê duyệt và kết quả xác minh.
- `models.py` định nghĩa dữ liệu kế hoạch và kết quả.

## Cài đặt

Yêu cầu Python 3.10 trở lên.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Cấu hình `.env`:

```dotenv
OPENROUTER_API_KEY=your_openrouter_api_key
OPENROUTER_MODEL=openai/gpt-4.1-mini
OPENROUTER_VISION_MODEL=openai/gpt-4.1-mini
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1

FILE_ORGANIZER_ALLOWED_ROOT=.

OPENROUTER_SITE_URL=http://localhost
OPENROUTER_APP_NAME=File Organizer Agent
```

`OPENROUTER_MODEL` phải hỗ trợ tool calling. Model dùng cho `OPENROUTER_VISION_MODEL` phải hỗ trợ ảnh đầu vào. Nếu không khai báo model thị giác, chương trình dùng lại `OPENROUTER_MODEL`.

## Chạy agent

Thư mục `sandbox/` đã có sẵn các tệp dùng để thử nghiệm. Chạy agent với bước phê duyệt:

```bash
python main.py ./sandbox
```

CLI sẽ hỏi:

```text
Approve this plan? [y/N]
```

Chỉ `y` hoặc `yes` cho phép executor chạy. Nhấn Enter, nhập `n` hoặc gặp EOF đều từ chối kế hoạch.

Truyền goal riêng:

```bash
python main.py ./sandbox \
  --goal "Tổ chức tài liệu theo dự án, đề xuất tên tệp rõ ràng hơn và không thay đổi gì trước khi tôi duyệt."
```

`--yes` là phê duyệt rõ ràng qua dòng lệnh:

```bash
python main.py ./sandbox --yes
```

Không nên dùng `--yes` khi chưa kiểm tra một kế hoạch mới.

## Kế hoạch tổ chức

Agent có thể đề xuất:

```text
CREATE
  Projects/
  Projects/Alpha/

MOVE + RENAME
  IMG_8291.jpg -> Projects/Alpha/architecture_diagram.jpg

MOVE
  report_final (1).pdf -> Duplicates/report_final (1).pdf

KEEP
  Notes/meeting_notes.docx

UNCERTAIN
  unknown.bin
```

Mỗi action có source, destination, lý do và độ tin cậy. Python yêu cầu kế hoạch xử lý đủ mọi tệp và từ chối đường dẫn không an toàn, destination trùng nhau hoặc đổi phần mở rộng.

## Giới hạn an toàn

- Chỉ xử lý target nằm bên trong `FILE_ORGANIZER_ALLOWED_ROOT`.
- Quét đệ quy tối đa 100 tệp và bỏ qua liên kết tượng trưng.
- Không xóa tệp.
- Không ghi đè destination đã tồn tại.
- Không cho phép đường dẫn tuyệt đối hoặc thành phần `..`.
- Cho phép đề xuất đổi tên nhưng phải giữ nguyên phần mở rộng.
- Kiểm tra lại SHA-256 trước mỗi lần di chuyển.
- `KEEP`, `UNCERTAIN` và `POSSIBLE_DUPLICATE` không được thực thi.
- Giới hạn vòng lặp agent ở 12 bước.
- `read_file` chỉ trả tối đa 2.000 ký tự và không đọc tệp lớn hơn 10 MB.
- Dữ liệu ảnh gửi cho model không vượt quá 5 MB.

Nếu một action thất bại, chương trình ghi nhận kết quả rồi tiếp tục với action còn lại. Bước xác minh kiểm tra destination, SHA-256 và mọi action bị `SKIP` hoặc `FAILED`.

## Audit log

Mỗi kế hoạch tạo một tệp trong `logs/`:

```text
logs/organizer_YYYYMMDD_HHMMSS_microseconds.log
```

Log ghi:

- goal
- công cụ agent đã gọi
- số request LLM
- kế hoạch
- quyết định phê duyệt
- kết quả từng action
- kết quả xác minh

Nội dung đọc từ tài liệu không được ghi vào trace.

## Kiểm thử

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

Test sử dụng client LLM giả lập nên không cần mạng hoặc API key. Bộ test kiểm tra vòng lặp tool calling, quét đệ quy, SHA-256, rename, thư mục nhiều cấp, no-overwrite, thay đổi nguồn sau khi lập kế hoạch và giới hạn sandbox.
