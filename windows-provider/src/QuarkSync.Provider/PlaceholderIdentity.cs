using System.Text.Json;

namespace QuarkSync.Provider;

internal sealed record PlaceholderIdentity(int SchemaVersion, string Path, string? Etag)
{
    internal byte[] Encode() => JsonSerializer.SerializeToUtf8Bytes(
        this, new JsonSerializerOptions { PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower });

    internal static PlaceholderIdentity Decode(ReadOnlySpan<byte> value) =>
        JsonSerializer.Deserialize<PlaceholderIdentity>(
            value,
            new JsonSerializerOptions
            {
                PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
                PropertyNameCaseInsensitive = true,
            }) ?? throw new InvalidDataException("Invalid placeholder identity");
}

