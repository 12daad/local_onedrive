using System.Buffers.Binary;
using System.IO.Pipes;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace QuarkSync.Provider.Ipc;

internal static class PipeProtocol
{
    internal const int Version = 1;
    internal const int MaxMessageBytes = 4 * 1024 * 1024;
    internal const string CorePipeName = "QuarkSync.Core.v1";
    internal const string ProviderPipeName = "QuarkSync.Provider.v1";

    internal static async Task WriteFrameAsync(Stream stream, object value, CancellationToken cancellationToken)
    {
        byte[] payload = JsonSerializer.SerializeToUtf8Bytes(value, JsonOptions);
        if (payload.Length > MaxMessageBytes)
        {
            throw new InvalidDataException("IPC message exceeds size limit");
        }

        byte[] header = new byte[4];
        BinaryPrimitives.WriteUInt32LittleEndian(header, (uint)payload.Length);
        await stream.WriteAsync(header, cancellationToken);
        await stream.WriteAsync(payload, cancellationToken);
        await stream.FlushAsync(cancellationToken);
    }

    internal static async Task<JsonObject> ReadFrameAsync(Stream stream, CancellationToken cancellationToken)
    {
        byte[] header = new byte[4];
        await ReadExactlyAsync(stream, header, cancellationToken);
        uint size = BinaryPrimitives.ReadUInt32LittleEndian(header);
        if (size > MaxMessageBytes)
        {
            throw new InvalidDataException("IPC message exceeds size limit");
        }

        byte[] payload = new byte[size];
        await ReadExactlyAsync(stream, payload, cancellationToken);
        return JsonNode.Parse(payload)?.AsObject()
            ?? throw new InvalidDataException("IPC JSON must be an object");
    }

    private static async Task ReadExactlyAsync(
        Stream stream, Memory<byte> buffer, CancellationToken cancellationToken)
    {
        int read = 0;
        while (read < buffer.Length)
        {
            int count = await stream.ReadAsync(buffer[read..], cancellationToken);
            if (count == 0)
            {
                throw new EndOfStreamException("Named pipe closed before the complete frame arrived");
            }
            read += count;
        }
    }

    internal static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        PropertyNameCaseInsensitive = true,
    };
}

