using System.IO.Pipes;
using System.Text.Json.Nodes;

namespace QuarkSync.Provider.Ipc;

internal sealed class CoreClient
{
    private readonly TimeSpan _connectTimeout;
    private readonly TimeSpan _responseTimeout;

    internal CoreClient(TimeSpan connectTimeout, TimeSpan responseTimeout)
    {
        _connectTimeout = connectTimeout;
        _responseTimeout = responseTimeout;
    }

    internal async Task<JsonObject> RequestAsync(
        string type,
        object? payload = null,
        CancellationToken cancellationToken = default,
        TimeSpan? responseTimeout = null)
    {
        await using NamedPipeClientStream pipe = new(
            ".", PipeProtocol.CorePipeName, PipeDirection.InOut, PipeOptions.Asynchronous);
        try
        {
            using CancellationTokenSource connect =
                CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            connect.CancelAfter(_connectTimeout);
            await pipe.ConnectAsync(connect.Token);
        }
        catch (OperationCanceledException exception) when (!cancellationToken.IsCancellationRequested)
        {
            throw new IOException("Timed out connecting to the QuarkSync daemon", exception);
        }

        string requestId = Guid.NewGuid().ToString();
        JsonObject response;
        try
        {
            using CancellationTokenSource request =
                CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            request.CancelAfter(responseTimeout ?? _responseTimeout);
            await PipeProtocol.WriteFrameAsync(
                pipe,
                new
                {
                    protocol_version = PipeProtocol.Version,
                    request_id = requestId,
                    type,
                    payload = payload ?? new { },
                },
                request.Token);
            response = await PipeProtocol.ReadFrameAsync(pipe, request.Token);
        }
        catch (OperationCanceledException exception) when (!cancellationToken.IsCancellationRequested)
        {
            throw new IOException($"QuarkSync daemon request timed out: {type}", exception);
        }
        if (response["request_id"]?.GetValue<string>() != requestId)
        {
            throw new InvalidDataException("Mismatched IPC response ID");
        }
        if (response["ok"]?.GetValue<bool>() != true)
        {
            JsonObject? error = response["error"] as JsonObject;
            throw new IOException(
                $"Core IPC failed: {error?["code"]} {error?["message"]}");
        }
        return response["result"]?.AsObject() ?? new JsonObject();
    }
}
