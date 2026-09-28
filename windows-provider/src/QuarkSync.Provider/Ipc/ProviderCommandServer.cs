using System.IO.Pipes;
using System.Text.Json.Nodes;

namespace QuarkSync.Provider.Ipc;

internal sealed class ProviderCommandServer : IAsyncDisposable
{
    private readonly Func<string, JsonObject, CancellationToken, Task<object?>> _handler;
    private readonly CancellationTokenSource _stop = new();
    private Task? _serverTask;

    internal ProviderCommandServer(
        Func<string, JsonObject, CancellationToken, Task<object?>> handler) => _handler = handler;

    internal void Start() => _serverTask = RunAsync(_stop.Token);

    private async Task RunAsync(CancellationToken cancellationToken)
    {
        while (!cancellationToken.IsCancellationRequested)
        {
            NamedPipeServerStream pipe = new(
                PipeProtocol.ProviderPipeName,
                PipeDirection.InOut,
                NamedPipeServerStream.MaxAllowedServerInstances,
                PipeTransmissionMode.Byte,
                PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly,
                64 * 1024,
                64 * 1024);
            try
            {
                await pipe.WaitForConnectionAsync(cancellationToken);
                _ = HandleAsync(pipe, cancellationToken);
            }
            catch (OperationCanceledException)
            {
                await pipe.DisposeAsync();
                break;
            }
        }
    }

    private async Task HandleAsync(NamedPipeServerStream pipe, CancellationToken cancellationToken)
    {
        await using (pipe)
        {
            string requestId = "unknown";
            try
            {
                JsonObject request = await PipeProtocol.ReadFrameAsync(pipe, cancellationToken);
                requestId = request["request_id"]?.GetValue<string>() ?? "unknown";
                if (request["protocol_version"]?.GetValue<int>() != PipeProtocol.Version)
                {
                    throw new InvalidDataException("Unsupported protocol version");
                }
                string type = request["type"]?.GetValue<string>()
                    ?? throw new InvalidDataException("Missing message type");
                JsonObject payload = request["payload"] as JsonObject ?? new JsonObject();
                object? result = await _handler(type, payload, cancellationToken);
                await PipeProtocol.WriteFrameAsync(
                    pipe,
                    new
                    {
                        protocol_version = PipeProtocol.Version,
                        request_id = requestId,
                        ok = true,
                        result = result ?? new { },
                    },
                    cancellationToken);
            }
            catch (Exception exception)
            {
                ProviderLog.Error("Provider command failed", exception);
                if (pipe.IsConnected)
                {
                    await PipeProtocol.WriteFrameAsync(
                        pipe,
                        new
                        {
                            protocol_version = PipeProtocol.Version,
                            request_id = requestId,
                            ok = false,
                            error = new
                            {
                                code = exception.GetType().Name,
                                message = exception.Message,
                            },
                        },
                        CancellationToken.None);
                }
            }
        }
    }

    public async ValueTask DisposeAsync()
    {
        _stop.Cancel();
        if (_serverTask is not null)
        {
            try { await _serverTask; } catch (OperationCanceledException) { }
        }
        _stop.Dispose();
    }
}

