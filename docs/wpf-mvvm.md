# WPF và MVVM

## 1. Window cơ bản với XAML

```xml
<Window x:Class="WpfApp.MainWindow"
        xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="MainWindow" Height="350" Width="525">
    <Grid Margin="10">
        <Grid.RowDefinitions>
            <RowDefinition Height="Auto" />
            <RowDefinition Height="*" />
        </Grid.RowDefinitions>

        <TextBox Grid.Row="0" Text="Hello WPF" Margin="0,0,0,10" />
        <Button Grid.Row="1" Content="Click Me" Width="100" HorizontalAlignment="Left" />
    </Grid>
</Window>
```

## 2. MVVM pattern

### ViewModel

```csharp
public class MainViewModel : INotifyPropertyChanged
{
    private string _greeting;
    public string Greeting
    {
        get => _greeting;
        set
        {
            _greeting = value;
            OnPropertyChanged();
        }
    }

    public ICommand ClickCommand { get; }

    public MainViewModel()
    {
        Greeting = "Hello MVVM";
        ClickCommand = new RelayCommand(OnClick);
    }

    private void OnClick()
    {
        Greeting = "Button clicked!";
    }

    public event PropertyChangedEventHandler PropertyChanged;
    protected void OnPropertyChanged([CallerMemberName] string name = null)
    {
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
    }
}
```

### RelayCommand

```csharp
public class RelayCommand : ICommand
{
    private readonly Action _execute;
    private readonly Func<bool> _canExecute;

    public RelayCommand(Action execute, Func<bool> canExecute = null)
    {
        _execute = execute;
        _canExecute = canExecute;
    }

    public bool CanExecute(object parameter) => _canExecute?.Invoke() ?? true;
    public void Execute(object parameter) => _execute();
    public event EventHandler CanExecuteChanged;
    public void RaiseCanExecuteChanged() => CanExecuteChanged?.Invoke(this, EventArgs.Empty);
}
```

### Data binding trong XAML

```xml
<Window.DataContext>
    <local:MainViewModel />
</Window.DataContext>

<StackPanel Margin="20">
    <TextBox Text="{Binding Greeting, UpdateSourceTrigger=PropertyChanged}" />
    <Button Content="Click" Command="{Binding ClickCommand}" Margin="0,10,0,0" />
    <TextBlock Text="{Binding Greeting}" FontSize="16" />
</StackPanel>
```

## 3. Styles và Resources

```xml
<Window.Resources>
    <Style TargetType="Button">
        <Setter Property="Background" Value="#4CAF50" />
        <Setter Property="Foreground" Value="White" />
        <Setter Property="Padding" Value="8" />
    </Style>
</Window.Resources>
```

## 4. Responsive layout

- Sử dụng `Grid`, `StackPanel`, `DockPanel`.
- Dùng `*` và `Auto` để tỉ lệ thay đổi.
- `Viewbox` giúp scale UI khi cần.
