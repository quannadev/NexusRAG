# Lợi ích của tính năng Custom Metadata cho Documents (PR Description)

## 📌 Tổng quan tính năng (Overview)
Tính năng này cho phép người dùng đính kèm các trường dữ liệu tùy chỉnh (Custom Metadata) dưới dạng Key-Value (ví dụ: `author: "John Doe"`, `category: "Finance"`, `year: "2023"`) ngay tại thời điểm upload tài liệu lên hệ thống NexusRAG. Dữ liệu này được lưu trữ trong Database (PostgreSQL) và toàn bộ các chunks trong Vector Database (ChromaDB).

## 🚀 Các lợi ích cốt lõi (Key Benefits)

### 1. Nâng cao độ chính xác của RAG thông qua Filter (Hybrid Search)
- **Vấn đề cũ:** Tìm kiếm Semantic Search (Vector) đôi khi trả về các chunks có kết quả đoạt độ tương đồng cao nhưng **sai ngữ cảnh** (ví dụ: tìm báo cáo tài chính năm 2023 nhưng chunk trả về lại là của 2022 do ngữ nghĩa giống nhau).
- **Lợi ích mới:** Nhờ có Custom Metadata lưu trực tiếp vào ChromaDB, hệ thống RAG giờ đây có thể thực hiện **Metadata Filtering** song song với Vector Search. 
  - *Ví dụ:* Query: *"Tìm doanh thu trong tài liệu."* + Lọc theo `{"year": "2023", "category": "finance"}`.
  - Kết quả trả về sẽ **chính xác tuyệt đối** theo ngữ cảnh mà người dùng khoanh vùng, giảm thiểu "ảo giác" (hallucination) của LLM do bị mớm sai tài liệu.

### 2. Tổ chức và quản lý tài liệu linh hoạt (Document Organization)
- Thay vì phải tạo vô số Knowledge Bases (Workspaces) nhỏ lẻ để phân loại tài liệu (ví dụ: Workspace "Báo_cáo_2022", Workspace "Báo_cáo_2023"...), người dùng có thể gom chung tất cả vào một Workspace dự án. 
- Việc phân loại, gom nhóm, và quản lý tài liệu giờ đây được thực hiện cực kì mượt mà thông qua việc đánh Tag (Metadata), mô phỏng lại cách các hệ quản trị nội dung (CMS/DMS) hiện đại hoạt động. 

### 3. Tối ưu hiệu năng truy xuất (Performance Optimization)
- Pre-filtering (lọc Metadata trước khi tính toán khoảng cách Vector) giúp ChromaDB thu hẹp không gian tìm kiếm (Search Space) đi đáng kể. 
- Thay vì phải quét qua hàng triệu vector chunks, database chỉ cần quét trên tập hợp nhỏ các chunks thỏa mãn Metadata. Điều này trực tiếp làm giảm độ trễ (latency) của toàn bộ quá trình Retrieval.

### 4. Mở đường cho các tính năng nâng cao trong tương lai
- **Phân quyền nâng cao (RBAC):** Có thể tận dụng metadata cấp document (ví dụ: `access_level: "confidential"`) để lọc quyền truy cập dữ liệu của từng User/Agent.
- **Data Analytics:** Dễ dàng cho phép UI tổng hợp thống kê theo phân loại tài liệu (Ví dụ: Workspace này có 10 tài liệu "Legal", 15 tài liệu "Tech").
- **Tương tác Multi-Agent:** Các Agents con có thể tự đưa ra quyết định "nên đọc file nào" dựa trên Metadata thay vì phải "mò mẫm" đọc toàn bộ nội dung text.

## 💻 UX/UI
- Tính năng được thiết kế thân thiện, tích hợp thẳng vào Data Panel, giúp thao tác thêm Key-Value diễn ra tự nhiên, không làm gián đoạn luồng Upload tài liệu của End-user.

## 🔌 Tích hợp API (API Integration)
Tính năng hỗ trợ Custom Metadata thông qua endpoint Upload Document hiện tại:
- **`POST /api/v1/documents/upload/{workspace_id}`**
  - Trong Data Form gửi lên, ngoài file đính kèm, FE có thể gửi thêm field `custom_metadata` (List các objects gồm `key` và `value`).
  - Hệ thống BE sẽ tự động convert lại thành JSON Object, validate cơ bản, và chèn vào Database (PostgreSQL) cũng như Vector Database (ChromaDB).

**Example cURL Upload:**
```bash
curl -X POST "http://localhost:8080/api/v1/documents/upload/1" \
  -H "accept: application/json" \
  -H "Content-Type: multipart/form-data" \
  -F "file=@/path/to/your/document.pdf" \
  -F 'custom_metadata=[{"key":"category","value":"finance"},{"key":"year","value":"2023"}]'
```

Hỗ trợ truy vấn và lọc Metadata Filtering qua các endpoint Query và Chat:
- **`POST /api/v1/query/{workspace_id}`**
- **`POST /api/v1/chat/{workspace_id}`**
  - Payload gửi lên sẽ hỗ trợ thêm tham số `metadata_filter` (dưới dạng JSON object chứa các điều kiện lọc Key-Value).
  - API sẽ sử dụng các filter này đẩy xuống lớp Vector Search (bằng `where` clause trong ChromaDB), thu gọn không gian tìm kiếm trước khi trả về kết quả cho LLM.

**Example cURL Query:**
```bash
curl -X POST "http://localhost:8080/api/v1/query/1" \
  -H "accept: application/json" \
  -H "Content-Type: application/json" \
  -d '{
    "question": "Tìm doanh thu năm nay.",
    "top_k": 3,
    "mode": "vector_only",
    "metadata_filter": {
      "category": "finance",
      "year": "2023"
    }
  }'
```
