# Frontend HTML/CSS/JS Basic Patterns

## 1. HTML structure chuẩn

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Responsive Frontend</title>
    <link rel="stylesheet" href="styles.css" />
</head>
<body>
    <header class="site-header">
        <h1>My Website</h1>
        <nav>
            <a href="#home">Home</a>
            <a href="#features">Features</a>
        </nav>
    </header>

    <main class="content">
        <section class="hero">
            <h2>Build great UI</h2>
            <p>Simple, responsive and accessible.</p>
        </section>
    </main>

    <footer class="site-footer">
        <p>© 2026 Example</p>
    </footer>
</body>
</html>
```

## 2. CSS responsive layout với Flexbox và Grid

```css
body {
    margin: 0;
    font-family: Arial, sans-serif;
    color: #333;
}

.site-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 16px;
    background: #222;
    color: white;
}

.content {
    display: grid;
    grid-template-columns: 1fr 300px;
    gap: 24px;
    padding: 24px;
}

@media (max-width: 768px) {
    .content {
        grid-template-columns: 1fr;
    }
}
```

## 3. JavaScript tương tác cơ bản

```html
<script>
    document.addEventListener('DOMContentLoaded', () => {
        const button = document.querySelector('.toggle-button');
        const menu = document.querySelector('.site-nav');

        button?.addEventListener('click', () => {
            menu?.classList.toggle('is-open');
        });
    });
</script>
```

## 4. Accessibility và best practice

- Sử dụng `aria-label`, `role`, và `alt` cho hình ảnh.
- Keyboard navigation: đảm bảo các nút/phím có thể dùng `Tab`.
- Contrast đủ mạnh cho văn bản.
- Sử dụng semantic HTML (`header`, `main`, `section`, `footer`).
