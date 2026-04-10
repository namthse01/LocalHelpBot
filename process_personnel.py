import json
import os
import urllib.request

# === CẤU HÌNH ===
INPUT_FILE = "raw_data.json"
OUTPUT_FILE = "output.json"
AVATAR_DIR = "personal_avatar"
# Thay base URL ảnh ở đây (ví dụ: "https://tms.tgl-cloud.com/uploads/")
IMAGE_BASE_URL = "https://tms.tgl-cloud.com/uploads/"
# ================

def extract_users(node, seen=None):
    if seen is None:
        seen = set()
    result = []

    for user in node.get("users", []):
        code = user.get("tglCode")
        if code and code not in seen:
            seen.add(code)
            result.append({
                "tglCode": code,
                "fullName": user.get("fullName"),
                "phone": user.get("phone"),
                "profileImageUrl": user.get("profileImageUrl"),
            })

    for child in node.get("children", []):
        result.extend(extract_users(child, seen))

    return result


def download_image(filename, dest_dir, base_url):
    if not filename:
        return
    url = base_url.rstrip("/") + "/" + filename
    dest_path = os.path.join(dest_dir, filename)
    os.makedirs(dest_dir, exist_ok=True)
    try:
        urllib.request.urlretrieve(url, dest_path)
        print(f"  OK: {filename}")
    except Exception as e:
        print(f"  FAIL: {filename} — {e}")


def main():
    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    # data có thể là list hoặc dict
    if isinstance(data, list):
        all_users = []
        seen = set()
        for node in data:
            all_users.extend(extract_users(node, seen))
    else:
        all_users = extract_users(data)

    # Tạo output.json chỉ gồm fullName và phone
    output = [
        {"fullName": u["fullName"], "phone": u["phone"]}
        for u in all_users
    ]

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n✓ Đã lưu {len(output)} người vào {OUTPUT_FILE}")

    # Tải ảnh
    print(f"\nĐang tải ảnh vào '{AVATAR_DIR}/'...")
    for u in all_users:
        download_image(u["profileImageUrl"], AVATAR_DIR, IMAGE_BASE_URL)

    print("\nXong!")


if __name__ == "__main__":
    main()
