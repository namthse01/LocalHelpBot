# Ollama là gì và dùng để làm gì?

**Ollama** là một nền tảng mã nguồn mở (MIT license) cho phép chạy các mô hình ngôn ngữ lớn (LLMs) trực tiếp trên máy tính cá nhân, không cần kết nối internet và đảm bảo bảo mật dữ liệu tuyệt đối [1]. Ollama đã phát triển từ một giao diện dòng lệnh đơn giản thành hệ sinh thái hoàn chỉnh, bao gồm khả năng chạy mô hình AI cục bộ, tích hợp API cho nhà phát triển, triển khai RAG (Retrieval-Augmented Generation) cho doanh nghiệp và cả cloud models cho tác vụ nặng [2].

## Điểm nổi bật của Ollama:

- **Dễ dàng cài đặt:** Hỗ trợ macOS, Windows và Linux.
- **Hơn 100 mô hình AI:** Bao gồm Llama 3.3, Gemma 4, DeepSeek-R1, Qwen 3, từ máy tính có 8GB RAM đến workstation GPU chuyên dụng [2].
- **Tương thích OpenAI API:** Nhà phát triển chỉ cần thay đổi hai dòng code để chuyển từ ChatGPT sang Ollama.
- **API tương thích OpenAI:** Dễ dàng tích hợp với Python, JavaScript, và cURL.

## Cách sử dụng

### 1. Cài đặt Ollama:

- Truy cập trang web của Ollama để tải phiên bản phù hợp với hệ điều hành của bạn.
- Kiểm tra cài đặt bằng lệnh `ollama --version`.

### 2. Chạy mô hình đầu tiên:

- Xem danh sách các mô hình tại ollama.com/models.
- Chạy mô hình bằng CLI, API cURL hoặc Python.

#### Ví dụ về cách chạy mô hình Gemma 3:

**Cách 1:** Sử dụng CLI:
```sh
ollama run gemma3
```

**Cách 2:** Sử dụng API cURL:
```sh
curl http://localhost:11434/api/chat -d '{
    "model": "gemma3",
    "messages": [{"role": "user", "content": "Hello there!"}],
    "stream": false
}'
```

**Cách 3:** Sử dụng Python:
```python
from ollama import chat

response = chat(model='gemma3', messages=[
    {'role': 'user', 'content': 'Why is the sky blue?'}
])
print(response.message.content)
```

## Tầm quan trọng của Ollama

Ollama cung cấp một giải pháp toàn diện cho việc chạy và tích hợp mô hình ngôn ngữ lớn, giúp người dùng tận dụng sức mạnh của AI ngay trên máy tính cá nhân. Điều này không chỉ mang lại sự linh hoạt trong việc sử dụng các mô hình AI mà còn đảm bảo bảo mật dữ liệu tuyệt đối [1].

## Ứng dụng

- **Nhà phát triển:** Dễ dàng tích hợp mô hình AI vào ứng dụng của mình thông qua API tương thích OpenAI.
- **Doanh nghiệp:** Triển khai RAG (Retrieval-Augmented Generation) cho các tác vụ phức tạp, đảm bảo hiệu suất và độ chính xác cao [2].
- **Người dùng cá nhân:** Sử dụng mô hình AI cục bộ để thực hiện các tác vụ hàng ngày như soạn thảo văn bản, trả lời câu hỏi, và nhiều hơn nữa.

## Kết luận

Ollama là một công cụ mạnh mẽ cho việc chạy và tích hợp mô hình ngôn ngữ lớn trên máy tính cá nhân. Với khả năng cài đặt dễ dàng, hỗ trợ đa nền tảng, và API tương thích OpenAI, Ollama mang lại trải nghiệm sử dụng AI linh hoạt và hiệu quả [1][2].

## Sources

[1] https://leos.vn/ollama-la-gi-huong-dan-chay-ai-local-mien-phi/

[2] https://fit.neu.edu.vn/post/ollama-giai-phap-toan-dien-cho-chay-va-tich-hop-mo-hinh-ngon-ngu-lon-ll-ms