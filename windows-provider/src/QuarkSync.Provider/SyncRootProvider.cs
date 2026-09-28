using System.Collections.Concurrent;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Text.Json.Nodes;
using QuarkSync.Provider.Interop;
using QuarkSync.Provider.Ipc;

namespace QuarkSync.Provider;

internal sealed class SyncRootProvider : IAsyncDisposable
{
    private static readonly Guid ProviderId = new("2B3DBDE4-2C45-4E34-909C-42862229C7E8");
    private const uint FileAttributeDirectory = 0x10;
    private const uint FileAttributeArchive = 0x20;
    private readonly string _root;
    private readonly CoreClient _core = new(
        TimeSpan.FromSeconds(EnvironmentSeconds("QUARKSYNC_IPC_CONNECT_TIMEOUT_SECONDS", 10, 1)),
        TimeSpan.FromSeconds(EnvironmentSeconds("QUARKSYNC_IPC_TIMEOUT_SECONDS", 120, 5)));
    private readonly TimeSpan _hydrationTimeout = TimeSpan.FromSeconds(
        EnvironmentSeconds("QUARKSYNC_HYDRATION_TIMEOUT_SECONDS", 86400, 60));
    private readonly ConcurrentDictionary<long, CancellationTokenSource> _transfers = new();
    private readonly ConcurrentDictionary<string, DateTimeOffset> _internalChanges = new(StringComparer.OrdinalIgnoreCase);
    private readonly ConcurrentDictionary<string, CancellationTokenSource> _watchDebounce = new(StringComparer.OrdinalIgnoreCase);
    private readonly CancellationTokenSource _stop = new();
    private readonly CfApi.CF_CALLBACK _fetchData;
    private readonly CfApi.CF_CALLBACK _cancelFetchData;
    private readonly CfApi.CF_CALLBACK _closeCompletion;
    private readonly CfApi.CF_CALLBACK _notifyDelete;
    private readonly CfApi.CF_CALLBACK _deleteCompletion;
    private readonly CfApi.CF_CALLBACK _notifyRename;
    private readonly CfApi.CF_CALLBACK _renameCompletion;
    private CfApi.CF_CONNECTION_KEY _connection;
    private bool _connected;
    private FileSystemWatcher? _watcher;
    private ProviderCommandServer? _commandServer;

    internal SyncRootProvider(string root)
    {
        _root = Path.GetFullPath(root);
        _fetchData = FetchDataCallback;
        _cancelFetchData = CancelFetchDataCallback;
        _closeCompletion = CloseCompletionCallback;
        _notifyDelete = NotifyDeleteCallback;
        _deleteCompletion = DeleteCompletionCallback;
        _notifyRename = NotifyRenameCallback;
        _renameCompletion = RenameCompletionCallback;
    }

    internal async Task RunAsync()
    {
        EnsureSupportedPlatform();
        Directory.CreateDirectory(_root);
        if (!await PackagedSyncRootRegistration.TryRegisterAsync(_root))
        {
            Register();
        }
        Connect();
        StartWatcher();
        _commandServer = new ProviderCommandServer(HandleCommandAsync);
        _commandServer.Start();
        ProviderLog.Info($"Provider connected to sync root {_root}");
        await Task.Delay(Timeout.Infinite, _stop.Token).ContinueWith(
            _ => { }, CancellationToken.None, TaskContinuationOptions.ExecuteSynchronously, TaskScheduler.Default);
    }

    private static void EnsureSupportedPlatform()
    {
        if (!OperatingSystem.IsWindowsVersionAtLeast(10, 0, 16299))
        {
            throw new PlatformNotSupportedException("QuarkSync requires Windows 10 version 1709 or later");
        }
        CfApi.ThrowIfFailed(CfApi.CfGetPlatformInfo(out CfApi.CF_PLATFORM_INFO info), "CfGetPlatformInfo");
        ProviderLog.Info(
            $"CFAPI platform build={info.BuildNumber} revision={info.RevisionNumber} integration={info.IntegrationNumber}");
    }

    private void Register()
    {
        byte[] identity = "QuarkSync/v1"u8.ToArray();
        GCHandle identityHandle = GCHandle.Alloc(identity, GCHandleType.Pinned);
        try
        {
            CfApi.CF_SYNC_REGISTRATION registration = new()
            {
                StructSize = (uint)Marshal.SizeOf<CfApi.CF_SYNC_REGISTRATION>(),
                ProviderName = "QuarkSync",
                ProviderVersion = "0.1.0",
                SyncRootIdentity = identityHandle.AddrOfPinnedObject(),
                SyncRootIdentityLength = (uint)identity.Length,
                FileIdentity = IntPtr.Zero,
                FileIdentityLength = 0,
                ProviderId = ProviderId,
            };
            CfApi.CF_SYNC_POLICIES policies = new()
            {
                StructSize = (uint)Marshal.SizeOf<CfApi.CF_SYNC_POLICIES>(),
                // FULL (2), AUTO_DEHYDRATION_ALLOWED (4)
                Hydration = new CfApi.CF_HYDRATION_POLICY { Primary = 2, Modifier = 4 },
                // Eager namespace enumeration means the local namespace is always fully populated.
                Population = new CfApi.CF_POPULATION_POLICY { Primary = 3, Modifier = 0 },
                InSync = CfApi.CF_INSYNC_POLICY.TrackFileCreationTime
                    | CfApi.CF_INSYNC_POLICY.TrackFileLastWriteTime
                    | CfApi.CF_INSYNC_POLICY.TrackDirectoryCreationTime
                    | CfApi.CF_INSYNC_POLICY.TrackDirectoryLastWriteTime,
                HardLink = 0,
                PlaceholderManagement = 0,
            };
            int result = CfApi.CfRegisterSyncRoot(
                _root, ref registration, ref policies,
                CfApi.CF_REGISTER_FLAGS.MarkInSyncOnRoot);
            if (result == CfApi.ErrorAlreadyExistsHResult)
            {
                result = CfApi.CfRegisterSyncRoot(
                    _root, ref registration, ref policies,
                    CfApi.CF_REGISTER_FLAGS.Update | CfApi.CF_REGISTER_FLAGS.MarkInSyncOnRoot);
            }
            CfApi.ThrowIfFailed(result, "CfRegisterSyncRoot");
        }
        finally
        {
            identityHandle.Free();
        }
    }

    private void Connect()
    {
        CfApi.CF_CALLBACK_REGISTRATION[] callbacks =
        [
            new() { Type = CfApi.CF_CALLBACK_TYPE.FetchData, Callback = _fetchData },
            new() { Type = CfApi.CF_CALLBACK_TYPE.CancelFetchData, Callback = _cancelFetchData },
            new() { Type = CfApi.CF_CALLBACK_TYPE.NotifyFileCloseCompletion, Callback = _closeCompletion },
            new() { Type = CfApi.CF_CALLBACK_TYPE.NotifyDelete, Callback = _notifyDelete },
            new() { Type = CfApi.CF_CALLBACK_TYPE.NotifyDeleteCompletion, Callback = _deleteCompletion },
            new() { Type = CfApi.CF_CALLBACK_TYPE.NotifyRename, Callback = _notifyRename },
            new() { Type = CfApi.CF_CALLBACK_TYPE.NotifyRenameCompletion, Callback = _renameCompletion },
            new() { Type = CfApi.CF_CALLBACK_TYPE.None, Callback = null },
        ];
        CfApi.ThrowIfFailed(
            CfApi.CfConnectSyncRoot(
                _root,
                callbacks,
                IntPtr.Zero,
                CfApi.CF_CONNECT_FLAGS.RequireFullFilePath
                    | CfApi.CF_CONNECT_FLAGS.RequireProcessInfo
                    | CfApi.CF_CONNECT_FLAGS.BlockSelfImplicitHydration,
                out _connection),
            "CfConnectSyncRoot");
        _connected = true;
    }

    private void FetchDataCallback(IntPtr callbackInfoPointer, IntPtr callbackParameters)
    {
        CfApi.CF_CALLBACK_INFO info = Marshal.PtrToStructure<CfApi.CF_CALLBACK_INFO>(callbackInfoPointer);
        TransferRequest request = new(
            info.ConnectionKey,
            info.TransferKey,
            info.RequestKey,
            info.FileSize,
            ReadInt64(callbackParameters, 16),
            ReadInt64(callbackParameters, 24),
            ReadIdentity(info),
            RelativePath(info));
        CancellationTokenSource cancellation = new();
        if (!_transfers.TryAdd(info.TransferKey, cancellation))
        {
            cancellation.Dispose();
            TransferFailure(request, CfApi.StatusCloudFileUnsuccessful);
            return;
        }
        _ = Task.Run(() => HydrateAsync(request, cancellation.Token));
    }

    private void CancelFetchDataCallback(IntPtr callbackInfoPointer, IntPtr callbackParameters)
    {
        CfApi.CF_CALLBACK_INFO info = Marshal.PtrToStructure<CfApi.CF_CALLBACK_INFO>(callbackInfoPointer);
        if (_transfers.TryGetValue(info.TransferKey, out CancellationTokenSource? cancellation))
        {
            cancellation.Cancel();
            _ = NotifyCoreAsync(
                "CANCEL_FETCH",
                new { transfer_id = info.TransferKey.ToString(System.Globalization.CultureInfo.InvariantCulture) });
        }
    }

    private async Task HydrateAsync(TransferRequest request, CancellationToken cancellationToken)
    {
        string? cacheKey = null;
        try
        {
            JsonObject result = await _core.RequestAsync(
                "FETCH_FILE",
                new
                {
                    path = request.Identity.Path,
                    etag = request.Identity.Etag,
                    transfer_id = request.TransferKey.ToString(
                        System.Globalization.CultureInfo.InvariantCulture),
                },
                cancellationToken,
                _hydrationTimeout);
            cacheKey = result["cache_key"]?.GetValue<string>()
                ?? throw new InvalidDataException("Core did not return a cache key");
            string cachePath = result["cache_path"]?.GetValue<string>()
                ?? throw new InvalidDataException("Core did not return a cache path");
            long size = result["size"]?.GetValue<long>()
                ?? throw new InvalidDataException("Core did not return file size");
            if (size != request.FileSize)
            {
                throw new InvalidDataException(
                    $"Hydration size changed: placeholder={request.FileSize}, remote={size}");
            }
            const int chunkSize = 1024 * 1024; // 4 KiB aligned.
            byte[] buffer = GC.AllocateUninitializedArray<byte>(chunkSize, pinned: true);
            await using FileStream stream = new(
                cachePath, FileMode.Open, FileAccess.Read, FileShare.Read,
                chunkSize, FileOptions.Asynchronous | FileOptions.SequentialScan);
            long offset = 0;
            while (offset < size)
            {
                cancellationToken.ThrowIfCancellationRequested();
                int wanted = (int)Math.Min(chunkSize, size - offset);
                int read = await stream.ReadAtLeastAsync(buffer.AsMemory(0, wanted), wanted, false, cancellationToken);
                unsafe
                {
                    fixed (byte* pointer = buffer)
                    {
                        TransferData(request, (IntPtr)pointer, offset, read, CfApi.StatusSuccess);
                    }
                }
                offset += read;
            }
            await _core.RequestAsync(
                "SYNC_STATE_CHANGED",
                new { path = request.Identity.Path, state = "HYDRATED", cache_key = cacheKey },
                cancellationToken);
        }
        catch (OperationCanceledException)
        {
            TransferFailure(request, CfApi.StatusCloudFileRequestAborted);
        }
        catch (IOException exception)
        {
            ProviderLog.Error($"Hydration network/cache failure for {request.Identity.Path}", exception);
            TransferFailure(request, CfApi.StatusCloudFileNetworkUnavailable);
        }
        catch (Exception exception)
        {
            ProviderLog.Error($"Hydration failed for {request.Identity.Path}", exception);
            TransferFailure(request, CfApi.StatusCloudFileUnsuccessful);
        }
        finally
        {
            if (_transfers.TryRemove(request.TransferKey, out CancellationTokenSource? cancellation))
            {
                cancellation.Dispose();
            }
        }
    }

    private static void TransferData(
        TransferRequest request, IntPtr buffer, long offset, long length, int completionStatus)
    {
        CfApi.CF_OPERATION_INFO operation = new()
        {
            StructSize = (uint)Marshal.SizeOf<CfApi.CF_OPERATION_INFO>(),
            Type = CfApi.CF_OPERATION_TYPE.TransferData,
            ConnectionKey = request.ConnectionKey,
            TransferKey = request.TransferKey,
            RequestKey = request.RequestKey,
        };
        CfApi.CF_OPERATION_PARAMETERS_TRANSFER_DATA parameters = new()
        {
            ParamSize = (uint)Marshal.SizeOf<CfApi.CF_OPERATION_PARAMETERS_TRANSFER_DATA>(),
            CompletionStatus = completionStatus,
            Buffer = buffer,
            Offset = offset,
            Length = length,
        };
        CfApi.ThrowIfFailed(CfApi.CfExecute(ref operation, ref parameters), "CfExecute(TRANSFER_DATA)");
    }

    private static void TransferFailure(TransferRequest request, int status)
    {
        try
        {
            TransferData(
                request,
                IntPtr.Zero,
                request.RequiredOffset,
                Math.Max(request.RequiredLength, 1),
                status);
        }
        catch (Exception exception)
        {
            ProviderLog.Error("Unable to complete failed hydration callback", exception);
        }
    }

    private void CloseCompletionCallback(IntPtr callbackInfoPointer, IntPtr callbackParameters)
    {
        CfApi.CF_CALLBACK_INFO info = Marshal.PtrToStructure<CfApi.CF_CALLBACK_INFO>(callbackInfoPointer);
        string path = RelativePath(info);
        if (IsInternal(path) || !Exists(path)) return;
        _ = Task.Run(async () =>
        {
            if (!IsInSync(path))
            {
                await NotifyCoreAsync("LOCAL_MODIFIED", new { path });
            }
        });
    }

    private void DeleteCompletionCallback(IntPtr callbackInfoPointer, IntPtr callbackParameters)
    {
        CfApi.CF_CALLBACK_INFO info = Marshal.PtrToStructure<CfApi.CF_CALLBACK_INFO>(callbackInfoPointer);
        string path = RelativePath(info);
        if (!IsInternal(path)) _ = NotifyCoreAsync("LOCAL_DELETED", new { path });
    }

    private void NotifyDeleteCallback(IntPtr callbackInfoPointer, IntPtr callbackParameters)
    {
        CfApi.CF_CALLBACK_INFO info = Marshal.PtrToStructure<CfApi.CF_CALLBACK_INFO>(callbackInfoPointer);
        Acknowledge(info, CfApi.CF_OPERATION_TYPE.AckDelete);
    }

    private void RenameCompletionCallback(IntPtr callbackInfoPointer, IntPtr callbackParameters)
    {
        CfApi.CF_CALLBACK_INFO info = Marshal.PtrToStructure<CfApi.CF_CALLBACK_INFO>(callbackInfoPointer);
        string destination = RelativePath(info);
        IntPtr sourcePointer = Marshal.ReadIntPtr(callbackParameters, 16);
        string sourceRaw = Marshal.PtrToStringUni(sourcePointer) ?? string.Empty;
        string source = ToRelative(sourceRaw);
        _ = NotifyCoreAsync("LOCAL_RENAMED", new { source, destination });
    }

    private void NotifyRenameCallback(IntPtr callbackInfoPointer, IntPtr callbackParameters)
    {
        CfApi.CF_CALLBACK_INFO info = Marshal.PtrToStructure<CfApi.CF_CALLBACK_INFO>(callbackInfoPointer);
        Acknowledge(info, CfApi.CF_OPERATION_TYPE.AckRename);
    }

    private static void Acknowledge(
        CfApi.CF_CALLBACK_INFO callback, CfApi.CF_OPERATION_TYPE operationType)
    {
        CfApi.CF_OPERATION_INFO operation = new()
        {
            StructSize = (uint)Marshal.SizeOf<CfApi.CF_OPERATION_INFO>(),
            Type = operationType,
            ConnectionKey = callback.ConnectionKey,
            TransferKey = callback.TransferKey,
            RequestKey = callback.RequestKey,
        };
        CfApi.CF_OPERATION_PARAMETERS_ACK parameters = new()
        {
            ParamSize = (uint)Marshal.SizeOf<CfApi.CF_OPERATION_PARAMETERS_ACK>(),
            CompletionStatus = CfApi.StatusSuccess,
        };
        CfApi.ThrowIfFailed(
            CfApi.CfExecuteAck(ref operation, ref parameters),
            operationType == CfApi.CF_OPERATION_TYPE.AckRename
                ? "CfExecute(ACK_RENAME)"
                : "CfExecute(ACK_DELETE)");
    }

    private async Task NotifyCoreAsync(string type, object payload)
    {
        try { await _core.RequestAsync(type, payload, _stop.Token); }
        catch (Exception exception) { ProviderLog.Error($"Failed to notify core: {type}", exception); }
    }

    private async Task<object?> HandleCommandAsync(
        string type, JsonObject payload, CancellationToken cancellationToken)
    {
        await Task.CompletedTask;
        switch (type)
        {
            case "PING":
                return new { service = "quarksync-provider", connected = _connected, root = _root };
            case "CREATE_PLACEHOLDERS":
                JsonArray entries = payload["entries"]?.AsArray()
                    ?? throw new InvalidDataException("Missing entries");
                int created = 0;
                foreach (JsonNode? node in entries.OrderBy(
                    item => item?["relative_path"]?.GetValue<string>().Count(character => character == '/') ?? 0))
                {
                    if (node is not null && CreatePlaceholder(node.AsObject())) created++;
                }
                return new { created };
            case "UPDATE_PLACEHOLDER":
                UpdatePlaceholder(payload);
                return new { updated = true };
            case "REMOVE_PLACEHOLDER":
                RemovePlaceholder(RequiredPath(payload));
                return new { removed = true };
            case "SET_IN_SYNC":
                SetInSync(RequiredPath(payload), payload["in_sync"]?.GetValue<bool>() == true);
                return new { updated = true };
            case "REPORT_PROGRESS":
                long transferKey = long.Parse(
                    payload["transfer_id"]?.GetValue<string>()
                        ?? throw new InvalidDataException("Missing transfer ID"),
                    System.Globalization.CultureInfo.InvariantCulture);
                long total = payload["total"]?.GetValue<long>()
                    ?? throw new InvalidDataException("Missing progress total");
                long completed = payload["completed"]?.GetValue<long>()
                    ?? throw new InvalidDataException("Missing completed progress");
                if (total <= 0 || completed < 0 || completed > total)
                {
                    throw new InvalidDataException("Invalid provider progress values");
                }
                if (!_transfers.ContainsKey(transferKey))
                {
                    return new { reported = false };
                }
                CfApi.ThrowIfFailed(
                    CfApi.CfReportProviderProgress(_connection, transferKey, total, completed),
                    "CfReportProviderProgress");
                return new { reported = true };
            case "HYDRATE":
                ChangeHydration(RequiredPath(payload), hydrate: true);
                return new { hydrated = true };
            case "DEHYDRATE":
                ChangeHydration(RequiredPath(payload), hydrate: false);
                return new { dehydrated = true };
            case "PIN":
                SetPin(RequiredPath(payload), true);
                return new { pinned = true };
            case "UNPIN":
                SetPin(RequiredPath(payload), false);
                return new { unpinned = true };
            case "STATUS":
                return new { connected = _connected, root = _root };
            case "STOP":
                _ = Task.Run(async () =>
                {
                    await Task.Delay(250);
                    _stop.Cancel();
                });
                return new { stopping = true };
            default:
                throw new InvalidDataException($"Unknown provider command {type}");
        }
    }

    private bool CreatePlaceholder(JsonObject value)
    {
        string relative = RequiredPath(value);
        string fullPath = FullPath(relative);
        if (File.Exists(fullPath) || Directory.Exists(fullPath)) return false;
        string? parent = Path.GetDirectoryName(fullPath);
        if (parent is null || !Directory.Exists(parent))
        {
            throw new DirectoryNotFoundException($"Placeholder parent is missing for {relative}");
        }
        bool directory = value["is_directory"]?.GetValue<bool>() == true;
        long size = value["size"]?.GetValue<long?>() ?? 0;
        string? etag = value["etag"]?.GetValue<string>();
        byte[] identity = new PlaceholderIdentity(1, relative.Replace('\\', '/'), etag).Encode();
        GCHandle identityHandle = GCHandle.Alloc(identity, GCHandleType.Pinned);
        try
        {
            DateTime timestamp = ParseTimestamp(value["mtime"]?.GetValue<string>());
            CfApi.CF_PLACEHOLDER_CREATE_INFO[] placeholders =
            [
                new()
                {
                    RelativeFileName = Path.GetFileName(fullPath),
                    FsMetadata = new CfApi.CF_FS_METADATA
                    {
                        BasicInfo = new CfApi.FILE_BASIC_INFO
                        {
                            CreationTime = timestamp.ToFileTimeUtc(),
                            LastAccessTime = timestamp.ToFileTimeUtc(),
                            LastWriteTime = timestamp.ToFileTimeUtc(),
                            ChangeTime = timestamp.ToFileTimeUtc(),
                            FileAttributes = directory ? FileAttributeDirectory : FileAttributeArchive,
                        },
                        FileSize = directory ? 0 : size,
                    },
                    FileIdentity = identityHandle.AddrOfPinnedObject(),
                    FileIdentityLength = (uint)identity.Length,
                    Flags = CfApi.CF_PLACEHOLDER_CREATE_FLAGS.MarkInSync
                        | (directory ? CfApi.CF_PLACEHOLDER_CREATE_FLAGS.DisableOnDemandPopulation : 0),
                },
            ];
            MarkInternal(relative);
            int result = CfApi.CfCreatePlaceholders(parent, placeholders, 1, 0, out uint processed);
            CfApi.ThrowIfFailed(result, $"CfCreatePlaceholders({relative})");
            if (processed != 1 || placeholders[0].Result < 0)
            {
                CfApi.ThrowIfFailed(placeholders[0].Result, $"placeholder result ({relative})");
            }
            return true;
        }
        finally
        {
            identityHandle.Free();
        }
    }

    private void UpdatePlaceholder(JsonObject value)
    {
        string relative = RequiredPath(value);
        string fullPath = FullPath(relative);
        if (!File.Exists(fullPath) && !Directory.Exists(fullPath))
        {
            CreatePlaceholder(value);
            return;
        }
        if (!IsInSync(relative))
        {
            throw new IOException($"Refusing to update dirty placeholder {relative}");
        }
        bool directory = value["is_directory"]?.GetValue<bool>() == true;
        DateTime timestamp = ParseTimestamp(value["mtime"]?.GetValue<string>());
        CfApi.CF_FS_METADATA metadata = new()
        {
            BasicInfo = new CfApi.FILE_BASIC_INFO
            {
                CreationTime = timestamp.ToFileTimeUtc(),
                LastAccessTime = timestamp.ToFileTimeUtc(),
                LastWriteTime = timestamp.ToFileTimeUtc(),
                ChangeTime = timestamp.ToFileTimeUtc(),
                FileAttributes = directory ? FileAttributeDirectory : FileAttributeArchive,
            },
            FileSize = directory ? 0 : value["size"]?.GetValue<long?>() ?? 0,
        };
        byte[] identity = new PlaceholderIdentity(
            1, relative, value["etag"]?.GetValue<string>()).Encode();
        GCHandle pin = GCHandle.Alloc(identity, GCHandleType.Pinned);
        try
        {
            using CfApi.SafeCfHandle handle = Open(relative, CfApi.CF_OPEN_FILE_FLAGS.Exclusive | CfApi.CF_OPEN_FILE_FLAGS.WriteAccess);
            MarkInternal(relative);
            CfApi.ThrowIfFailed(
                CfApi.CfUpdatePlaceholder(
                    handle, ref metadata, pin.AddrOfPinnedObject(), (uint)identity.Length,
                    IntPtr.Zero, 0,
                    CfApi.CF_UPDATE_FLAGS.MarkInSync | (directory ? 0 : CfApi.CF_UPDATE_FLAGS.Dehydrate),
                    IntPtr.Zero, IntPtr.Zero),
                $"CfUpdatePlaceholder({relative})");
        }
        finally { pin.Free(); }
    }

    private void RemovePlaceholder(string relative)
    {
        string full = FullPath(relative);
        if (Exists(relative) && !IsInSync(relative))
        {
            throw new IOException($"Refusing to remove dirty placeholder {relative}");
        }
        MarkInternal(relative);
        if (Directory.Exists(full)) Directory.Delete(full, recursive: false);
        else File.Delete(full);
    }

    private void SetInSync(string relative, bool inSync)
    {
        using CfApi.SafeCfHandle handle = Open(
            relative, CfApi.CF_OPEN_FILE_FLAGS.Exclusive | CfApi.CF_OPEN_FILE_FLAGS.WriteAccess);
        if (!IsPlaceholder(handle))
        {
            byte[] identity = new PlaceholderIdentity(1, relative, null).Encode();
            GCHandle pin = GCHandle.Alloc(identity, GCHandleType.Pinned);
            try
            {
                CfApi.ThrowIfFailed(
                    CfApi.CfConvertToPlaceholder(
                        handle, pin.AddrOfPinnedObject(), (uint)identity.Length,
                        inSync ? CfApi.CF_CONVERT_FLAGS.MarkInSync : CfApi.CF_CONVERT_FLAGS.None,
                        out _, IntPtr.Zero),
                    $"CfConvertToPlaceholder({relative})");
            }
            finally { pin.Free(); }
        }
        MarkInternal(relative);
        CfApi.ThrowIfFailed(
            CfApi.CfSetInSyncState(
                handle,
                inSync ? CfApi.CF_IN_SYNC_STATE.InSync : CfApi.CF_IN_SYNC_STATE.NotInSync,
                0,
                IntPtr.Zero),
            $"CfSetInSyncState({relative})");
    }

    private void ChangeHydration(string relative, bool hydrate)
    {
        using CfApi.SafeCfHandle handle = Open(
            relative, CfApi.CF_OPEN_FILE_FLAGS.Exclusive | CfApi.CF_OPEN_FILE_FLAGS.Foreground);
        int result = hydrate
            ? CfApi.CfHydratePlaceholder(handle, 0, CfApi.CfEof, 0, IntPtr.Zero)
            : CfApi.CfDehydratePlaceholder(handle, 0, CfApi.CfEof, 0, IntPtr.Zero);
        CfApi.ThrowIfFailed(result, hydrate ? "CfHydratePlaceholder" : "CfDehydratePlaceholder");
    }

    private void SetPin(string relative, bool pinned)
    {
        using CfApi.SafeCfHandle handle = Open(relative, CfApi.CF_OPEN_FILE_FLAGS.None);
        CfApi.CF_SET_PIN_FLAGS flags = Directory.Exists(FullPath(relative))
            ? CfApi.CF_SET_PIN_FLAGS.Recurse
            : CfApi.CF_SET_PIN_FLAGS.None;
        CfApi.ThrowIfFailed(
            CfApi.CfSetPinState(
                handle,
                pinned ? CfApi.CF_PIN_STATE.Pinned : CfApi.CF_PIN_STATE.Unpinned,
                flags,
                IntPtr.Zero),
            "CfSetPinState");
    }

    private void StartWatcher()
    {
        _watcher = new FileSystemWatcher(_root)
        {
            IncludeSubdirectories = true,
            NotifyFilter = NotifyFilters.FileName | NotifyFilters.DirectoryName
                | NotifyFilters.LastWrite | NotifyFilters.Size,
            InternalBufferSize = 64 * 1024,
            EnableRaisingEvents = true,
        };
        _watcher.Created += (_, args) => DebounceLocal(args.FullPath, "LOCAL_CREATED");
        _watcher.Changed += (_, args) => DebounceLocal(args.FullPath, "LOCAL_MODIFIED");
        _watcher.Deleted += (_, args) => WatchDelete(args.FullPath);
        _watcher.Renamed += (_, args) => WatchRename(args.OldFullPath, args.FullPath);
        _watcher.Error += (_, args) => ProviderLog.Error("FileSystemWatcher overflow/error", args.GetException());
    }

    private void DebounceLocal(string fullPath, string type)
    {
        string relative;
        try { relative = ToRelative(fullPath); }
        catch { return; }
        bool directory = Directory.Exists(fullPath);
        if (IsInternal(relative) || (directory && type != "LOCAL_CREATED")) return;
        CancellationTokenSource current = new();
        _watchDebounce.AddOrUpdate(
            relative,
            current,
            (_, previous) => { previous.Cancel(); previous.Dispose(); return current; });
        _ = Task.Run(async () =>
        {
            try
            {
                await Task.Delay(TimeSpan.FromSeconds(1), current.Token);
                bool stillDirectory = Directory.Exists(fullPath);
                if ((File.Exists(fullPath) || stillDirectory)
                    && !IsInternal(relative) && !IsInSync(relative))
                {
                    await NotifyCoreAsync(type, new { path = relative, is_directory = stillDirectory });
                }
            }
            catch (OperationCanceledException) { }
            finally
            {
                _watchDebounce.TryRemove(new KeyValuePair<string, CancellationTokenSource>(relative, current));
                current.Dispose();
            }
        });
    }

    private void WatchDelete(string fullPath)
    {
        string relative;
        try { relative = ToRelative(fullPath); } catch { return; }
        if (!IsInternal(relative)) _ = NotifyCoreAsync("LOCAL_DELETED", new { path = relative });
    }

    private void WatchRename(string oldFullPath, string newFullPath)
    {
        string source;
        string destination;
        try { source = ToRelative(oldFullPath); destination = ToRelative(newFullPath); }
        catch { return; }
        _ = NotifyCoreAsync("LOCAL_RENAMED", new { source, destination });
    }

    private bool IsInSync(string relative)
    {
        try
        {
            using CfApi.SafeCfHandle handle = Open(relative, CfApi.CF_OPEN_FILE_FLAGS.None);
            const uint bufferLength = 8192;
            IntPtr buffer = Marshal.AllocHGlobal((int)bufferLength);
            try
            {
                int result = CfApi.CfGetPlaceholderInfo(
                    handle, 0, buffer, bufferLength, out _);
                if (result < 0) return false;
                uint inSync = unchecked((uint)Marshal.ReadInt32(buffer, 4));
                return inSync == (uint)CfApi.CF_IN_SYNC_STATE.InSync;
            }
            finally { Marshal.FreeHGlobal(buffer); }
        }
        catch { return false; }
    }

    private static bool IsPlaceholder(CfApi.SafeCfHandle handle)
    {
        const uint bufferLength = 8192;
        IntPtr buffer = Marshal.AllocHGlobal((int)bufferLength);
        try
        {
            return CfApi.CfGetPlaceholderInfo(handle, 0, buffer, bufferLength, out _) >= 0;
        }
        finally { Marshal.FreeHGlobal(buffer); }
    }

    private CfApi.SafeCfHandle Open(string relative, CfApi.CF_OPEN_FILE_FLAGS flags)
    {
        CfApi.ThrowIfFailed(CfApi.CfOpenFileWithOplock(FullPath(relative), flags, out CfApi.SafeCfHandle handle),
            $"CfOpenFileWithOplock({relative})");
        return handle;
    }

    private PlaceholderIdentity ReadIdentity(CfApi.CF_CALLBACK_INFO info)
    {
        if (info.FileIdentity == IntPtr.Zero || info.FileIdentityLength == 0)
        {
            return new PlaceholderIdentity(1, RelativePath(info), null);
        }
        byte[] identity = new byte[info.FileIdentityLength];
        Marshal.Copy(info.FileIdentity, identity, 0, identity.Length);
        return PlaceholderIdentity.Decode(identity);
    }

    private string RelativePath(CfApi.CF_CALLBACK_INFO info)
    {
        string raw = Marshal.PtrToStringUni(info.NormalizedPath) ?? string.Empty;
        return ToRelative(raw);
    }

    private string ToRelative(string value)
    {
        string path = Path.IsPathRooted(value) ? Path.GetRelativePath(_root, value) : value.TrimStart('\\', '/');
        if (path == ".." || path.StartsWith(".." + Path.DirectorySeparatorChar, StringComparison.Ordinal))
        {
            throw new InvalidDataException("Callback path escapes the sync root");
        }
        return path.Replace('\\', '/');
    }

    private string FullPath(string relative)
    {
        string full = Path.GetFullPath(Path.Combine(_root, relative.Replace('/', Path.DirectorySeparatorChar)));
        if (!full.StartsWith(_root.TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar,
                StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidDataException("Path escapes sync root");
        }
        return full;
    }

    private static string RequiredPath(JsonObject payload) =>
        payload["path"]?.GetValue<string>() ?? payload["relative_path"]?.GetValue<string>()
        ?? throw new InvalidDataException("Missing path");

    private static DateTime ParseTimestamp(string? value) =>
        DateTimeOffset.TryParse(value, out DateTimeOffset parsed)
            ? parsed.UtcDateTime
            : DateTime.UtcNow;

    private static int EnvironmentSeconds(string name, int fallback, int minimum)
    {
        string? raw = Environment.GetEnvironmentVariable(name);
        return int.TryParse(raw, out int value) && value >= minimum ? value : fallback;
    }

    private void MarkInternal(string relative) =>
        _internalChanges[relative] = DateTimeOffset.UtcNow.AddSeconds(5);

    private bool IsInternal(string relative)
    {
        if (!_internalChanges.TryGetValue(relative, out DateTimeOffset until)) return false;
        if (until >= DateTimeOffset.UtcNow) return true;
        _internalChanges.TryRemove(relative, out _);
        return false;
    }

    private bool Exists(string relative) =>
        File.Exists(FullPath(relative)) || Directory.Exists(FullPath(relative));

    private static long ReadInt64(IntPtr pointer, int offset) => Marshal.ReadInt64(pointer, offset);

    public async ValueTask DisposeAsync()
    {
        _stop.Cancel();
        _watcher?.Dispose();
        foreach (CancellationTokenSource transfer in _transfers.Values) transfer.Cancel();
        if (_commandServer is not null) await _commandServer.DisposeAsync();
        if (_connected)
        {
            CfApi.ThrowIfFailed(CfApi.CfDisconnectSyncRoot(_connection), "CfDisconnectSyncRoot");
            _connected = false;
        }
        _stop.Dispose();
    }

    private sealed record TransferRequest(
        CfApi.CF_CONNECTION_KEY ConnectionKey,
        long TransferKey,
        long RequestKey,
        long FileSize,
        long RequiredOffset,
        long RequiredLength,
        PlaceholderIdentity Identity,
        string CallbackPath);
}
