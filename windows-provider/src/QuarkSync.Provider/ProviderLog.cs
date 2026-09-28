using System.Text.RegularExpressions;

namespace QuarkSync.Provider;

internal static partial class ProviderLog
{
    private static readonly object Gate = new();
    private static string? _path;
    private const long MaxBytes = 10 * 1024 * 1024;

    internal static void Initialize()
    {
        string directory = Environment.GetEnvironmentVariable("QUARKSYNC_LOG_DIR")
            ?? Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "QuarkSync", "logs");
        Directory.CreateDirectory(directory);
        _path = Path.Combine(directory, "provider.log");
    }

    internal static void Info(string message) => Write("INFO", message, null);
    internal static void Warning(string message) => Write("WARNING", message, null);
    internal static void Error(string message, Exception? exception = null) =>
        Write("ERROR", message, exception);

    private static void Write(string level, string message, Exception? exception)
    {
        if (_path is null) return;
        string line = $"{DateTimeOffset.UtcNow:O} {level} {Redact(message)}";
        if (exception is not null)
        {
            line += $" {exception.GetType().Name}: {Redact(exception.Message)}";
        }
        lock (Gate)
        {
            if (File.Exists(_path) && new FileInfo(_path).Length >= MaxBytes)
            {
                string old = _path + ".1";
                File.Move(_path, old, true);
            }
            File.AppendAllText(_path, line + Environment.NewLine);
        }
    }

    private static string Redact(string value) => SecretPattern().Replace(value, "$1=<redacted>");

    [GeneratedRegex("(?i)(authorization|cookie|password|token)\\s*[:=]\\s*[^\\s,;]+")]
    private static partial Regex SecretPattern();
}

