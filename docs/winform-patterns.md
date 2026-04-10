# WinForms Patterns

## 1. Form cơ bản với DataGridView và Binding

```csharp
public partial class MainForm : Form
{
    private BindingList<Person> _people;

    public MainForm()
    {
        InitializeComponent();
        _people = new BindingList<Person>
        {
            new Person { Name = "Alice", Age = 30 },
            new Person { Name = "Bob", Age = 28 }
        };
        dataGridView1.DataSource = _people;
    }
}

public class Person
{
    public string Name { get; set; }
    public int Age { get; set; }
}
```

### 2. Layout chuẩn với TableLayoutPanel

- Sử dụng `TableLayoutPanel` để chia vùng, giữ form responsive.
- `Dock = Fill` cho controls quan trọng.

```csharp
var table = new TableLayoutPanel
{
    ColumnCount = 2,
    RowCount = 3,
    Dock = DockStyle.Fill,
};
table.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 30));
table.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 70));

var label = new Label { Text = "Name:", TextAlign = ContentAlignment.MiddleRight, Dock = DockStyle.Fill };
var textBox = new TextBox { Dock = DockStyle.Fill };

table.Controls.Add(label, 0, 0);
table.Controls.Add(textBox, 1, 0);
```

### 3. Event handling

```csharp
buttonSave.Click += (sender, e) =>
{
    MessageBox.Show("Save clicked!");
};
```

### 4. Custom control đơn giản

```csharp
public class LabeledTextBox : UserControl
{
    public Label Label { get; }
    public TextBox TextBox { get; }

    public string LabelText
    {
        get => Label.Text;
        set => Label.Text = value;
    }

    public string TextValue
    {
        get => TextBox.Text;
        set => TextBox.Text = value;
    }

    public LabeledTextBox()
    {
        Label = new Label { Dock = DockStyle.Left, Width = 80, TextAlign = ContentAlignment.MiddleRight };
        TextBox = new TextBox { Dock = DockStyle.Fill };
        Controls.Add(TextBox);
        Controls.Add(Label);
    }
}
```
