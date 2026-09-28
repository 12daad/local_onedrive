using System.Runtime.InteropServices;

namespace QuarkSync.Provider.Interop;

internal static class CfApi
{
    internal const int S_OK = 0;
    internal const int ErrorAlreadyExistsHResult = unchecked((int)0x800700B7);
    internal const int StatusSuccess = 0;
    internal const int StatusCloudFileNetworkUnavailable = unchecked((int)0xC000CF11);
    internal const int StatusCloudFileUnsuccessful = unchecked((int)0xC000CF12);
    internal const int StatusCloudFileRequestAborted = unchecked((int)0xC000CF16);
    internal const long CfEof = -1;

    [StructLayout(LayoutKind.Sequential)]
    internal struct CF_PLATFORM_INFO
    {
        internal uint BuildNumber;
        internal uint RevisionNumber;
        internal uint IntegrationNumber;
    }

    [StructLayout(LayoutKind.Sequential)]
    internal struct CF_CONNECTION_KEY { internal long Internal; }

    [StructLayout(LayoutKind.Sequential)]
    internal struct FILE_BASIC_INFO
    {
        internal long CreationTime;
        internal long LastAccessTime;
        internal long LastWriteTime;
        internal long ChangeTime;
        internal uint FileAttributes;
    }

    [StructLayout(LayoutKind.Sequential)]
    internal struct CF_FS_METADATA
    {
        internal FILE_BASIC_INFO BasicInfo;
        internal long FileSize;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    internal struct CF_PLACEHOLDER_CREATE_INFO
    {
        [MarshalAs(UnmanagedType.LPWStr)] internal string RelativeFileName;
        internal CF_FS_METADATA FsMetadata;
        internal IntPtr FileIdentity;
        internal uint FileIdentityLength;
        internal CF_PLACEHOLDER_CREATE_FLAGS Flags;
        internal int Result;
        internal long CreateUsn;
    }

    [StructLayout(LayoutKind.Sequential)]
    internal struct CF_HYDRATION_POLICY
    {
        internal ushort Primary;
        internal ushort Modifier;
    }

    [StructLayout(LayoutKind.Sequential)]
    internal struct CF_POPULATION_POLICY
    {
        internal ushort Primary;
        internal ushort Modifier;
    }

    [StructLayout(LayoutKind.Sequential)]
    internal struct CF_SYNC_POLICIES
    {
        internal uint StructSize;
        internal CF_HYDRATION_POLICY Hydration;
        internal CF_POPULATION_POLICY Population;
        internal CF_INSYNC_POLICY InSync;
        internal uint HardLink;
        internal uint PlaceholderManagement;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    internal struct CF_SYNC_REGISTRATION
    {
        internal uint StructSize;
        [MarshalAs(UnmanagedType.LPWStr)] internal string ProviderName;
        [MarshalAs(UnmanagedType.LPWStr)] internal string ProviderVersion;
        internal IntPtr SyncRootIdentity;
        internal uint SyncRootIdentityLength;
        internal IntPtr FileIdentity;
        internal uint FileIdentityLength;
        internal Guid ProviderId;
    }

    [UnmanagedFunctionPointer(CallingConvention.Winapi)]
    internal delegate void CF_CALLBACK(IntPtr callbackInfo, IntPtr callbackParameters);

    [StructLayout(LayoutKind.Sequential)]
    internal struct CF_CALLBACK_REGISTRATION
    {
        internal CF_CALLBACK_TYPE Type;
        internal CF_CALLBACK? Callback;
    }

    [StructLayout(LayoutKind.Sequential)]
    internal struct CF_CALLBACK_INFO
    {
        internal uint StructSize;
        internal CF_CONNECTION_KEY ConnectionKey;
        internal IntPtr CallbackContext;
        internal IntPtr VolumeGuidName;
        internal IntPtr VolumeDosName;
        internal uint VolumeSerialNumber;
        internal long SyncRootFileId;
        internal IntPtr SyncRootIdentity;
        internal uint SyncRootIdentityLength;
        internal long FileId;
        internal long FileSize;
        internal IntPtr FileIdentity;
        internal uint FileIdentityLength;
        internal IntPtr NormalizedPath;
        internal long TransferKey;
        internal byte PriorityHint;
        internal IntPtr CorrelationVector;
        internal IntPtr ProcessInfo;
        internal long RequestKey;
    }

    [StructLayout(LayoutKind.Sequential)]
    internal struct CF_OPERATION_INFO
    {
        internal uint StructSize;
        internal CF_OPERATION_TYPE Type;
        internal CF_CONNECTION_KEY ConnectionKey;
        internal long TransferKey;
        internal IntPtr CorrelationVector;
        internal IntPtr SyncStatus;
        internal long RequestKey;
    }

    [StructLayout(LayoutKind.Explicit, Size = 40)]
    internal struct CF_OPERATION_PARAMETERS_TRANSFER_DATA
    {
        [FieldOffset(0)] internal uint ParamSize;
        [FieldOffset(8)] internal uint Flags;
        [FieldOffset(12)] internal int CompletionStatus;
        [FieldOffset(16)] internal IntPtr Buffer;
        [FieldOffset(24)] internal long Offset;
        [FieldOffset(32)] internal long Length;
    }

    [StructLayout(LayoutKind.Explicit, Size = 16)]
    internal struct CF_OPERATION_PARAMETERS_ACK
    {
        [FieldOffset(0)] internal uint ParamSize;
        [FieldOffset(8)] internal uint Flags;
        [FieldOffset(12)] internal int CompletionStatus;
    }

    [Flags]
    internal enum CF_REGISTER_FLAGS : uint
    {
        None = 0,
        Update = 1,
        MarkInSyncOnRoot = 4,
    }

    [Flags]
    internal enum CF_CONNECT_FLAGS : uint
    {
        None = 0,
        RequireProcessInfo = 2,
        RequireFullFilePath = 4,
        BlockSelfImplicitHydration = 8,
    }

    internal enum CF_CALLBACK_TYPE : uint
    {
        FetchData = 0,
        ValidateData = 1,
        CancelFetchData = 2,
        FetchPlaceholders = 3,
        CancelFetchPlaceholders = 4,
        NotifyFileOpenCompletion = 5,
        NotifyFileCloseCompletion = 6,
        NotifyDehydrate = 7,
        NotifyDehydrateCompletion = 8,
        NotifyDelete = 9,
        NotifyDeleteCompletion = 10,
        NotifyRename = 11,
        NotifyRenameCompletion = 12,
        None = 0xffffffff,
    }

    internal enum CF_OPERATION_TYPE : uint
    {
        TransferData = 0,
        RetrieveData = 1,
        AckData = 2,
        RestartHydration = 3,
        TransferPlaceholders = 4,
        AckDehydrate = 5,
        AckDelete = 6,
        AckRename = 7,
    }

    [Flags]
    internal enum CF_PLACEHOLDER_CREATE_FLAGS : uint
    {
        None = 0,
        DisableOnDemandPopulation = 1,
        MarkInSync = 2,
        Supersede = 4,
    }

    [Flags]
    internal enum CF_INSYNC_POLICY : uint
    {
        None = 0,
        TrackFileCreationTime = 1,
        TrackFileLastWriteTime = 0x100,
        TrackDirectoryCreationTime = 0x10,
        TrackDirectoryLastWriteTime = 0x200,
    }

    internal enum CF_IN_SYNC_STATE : uint { NotInSync = 0, InSync = 1 }
    internal enum CF_PIN_STATE : uint { Unspecified = 0, Pinned = 1, Unpinned = 2, Excluded = 3, Inherit = 4 }

    [Flags]
    internal enum CF_SET_PIN_FLAGS : uint { None = 0, Recurse = 1 }
    [Flags]
    internal enum CF_OPEN_FILE_FLAGS : uint { None = 0, Exclusive = 1, WriteAccess = 2, DeleteAccess = 4, Foreground = 8 }
    [Flags]
    internal enum CF_UPDATE_FLAGS : uint { None = 0, MarkInSync = 2, Dehydrate = 4, ClearInSync = 0x40 }
    [Flags]
    internal enum CF_CONVERT_FLAGS : uint { None = 0, MarkInSync = 1, Dehydrate = 2 }

    [DllImport("CldApi.dll")]
    internal static extern int CfGetPlatformInfo(out CF_PLATFORM_INFO platformVersion);

    [DllImport("CldApi.dll", CharSet = CharSet.Unicode)]
    internal static extern int CfRegisterSyncRoot(
        string syncRootPath,
        ref CF_SYNC_REGISTRATION registration,
        ref CF_SYNC_POLICIES policies,
        CF_REGISTER_FLAGS registerFlags);

    [DllImport("CldApi.dll", CharSet = CharSet.Unicode)]
    internal static extern int CfUnregisterSyncRoot(string syncRootPath);

    [DllImport("CldApi.dll", CharSet = CharSet.Unicode)]
    internal static extern int CfConnectSyncRoot(
        string syncRootPath,
        [In] CF_CALLBACK_REGISTRATION[] callbackTable,
        IntPtr callbackContext,
        CF_CONNECT_FLAGS connectFlags,
        out CF_CONNECTION_KEY connectionKey);

    [DllImport("CldApi.dll")]
    internal static extern int CfDisconnectSyncRoot(CF_CONNECTION_KEY connectionKey);

    [DllImport("CldApi.dll")]
    internal static extern int CfReportProviderProgress(
        CF_CONNECTION_KEY connectionKey,
        long transferKey,
        long providerProgressTotal,
        long providerProgressCompleted);

    [DllImport("CldApi.dll", CharSet = CharSet.Unicode)]
    internal static extern int CfCreatePlaceholders(
        string baseDirectoryPath,
        [In, Out] CF_PLACEHOLDER_CREATE_INFO[] placeholders,
        uint placeholderCount,
        uint createFlags,
        out uint entriesProcessed);

    [DllImport("CldApi.dll")]
    internal static extern int CfExecute(
        ref CF_OPERATION_INFO operationInfo,
        ref CF_OPERATION_PARAMETERS_TRANSFER_DATA operationParameters);

    [DllImport("CldApi.dll", EntryPoint = "CfExecute")]
    internal static extern int CfExecuteAck(
        ref CF_OPERATION_INFO operationInfo,
        ref CF_OPERATION_PARAMETERS_ACK operationParameters);

    [DllImport("CldApi.dll", CharSet = CharSet.Unicode)]
    internal static extern int CfOpenFileWithOplock(
        string filePath, CF_OPEN_FILE_FLAGS flags, out SafeCfHandle protectedHandle);

    [DllImport("CldApi.dll")]
    internal static extern int CfSetInSyncState(
        SafeCfHandle fileHandle, CF_IN_SYNC_STATE inSyncState, uint inSyncFlags, IntPtr inSyncUsn);

    [DllImport("CldApi.dll")]
    internal static extern int CfSetPinState(
        SafeCfHandle fileHandle, CF_PIN_STATE pinState, CF_SET_PIN_FLAGS pinFlags, IntPtr overlapped);

    [DllImport("CldApi.dll")]
    internal static extern int CfHydratePlaceholder(
        SafeCfHandle fileHandle, long startingOffset, long length, uint hydrateFlags, IntPtr overlapped);

    [DllImport("CldApi.dll")]
    internal static extern int CfDehydratePlaceholder(
        SafeCfHandle fileHandle, long startingOffset, long length, uint dehydrateFlags, IntPtr overlapped);

    [DllImport("CldApi.dll")]
    internal static extern int CfUpdatePlaceholder(
        SafeCfHandle fileHandle,
        ref CF_FS_METADATA fsMetadata,
        IntPtr fileIdentity,
        uint fileIdentityLength,
        IntPtr dehydrateRanges,
        uint dehydrateRangeCount,
        CF_UPDATE_FLAGS updateFlags,
        IntPtr updateUsn,
        IntPtr overlapped);

    [DllImport("CldApi.dll")]
    internal static extern int CfConvertToPlaceholder(
        SafeCfHandle fileHandle,
        IntPtr fileIdentity,
        uint fileIdentityLength,
        CF_CONVERT_FLAGS convertFlags,
        out long convertUsn,
        IntPtr overlapped);

    [DllImport("CldApi.dll")]
    internal static extern int CfGetPlaceholderInfo(
        SafeCfHandle fileHandle,
        uint infoClass,
        IntPtr infoBuffer,
        uint infoBufferLength,
        out uint returnedLength);

    [DllImport("CldApi.dll")]
    internal static extern void CfCloseHandle(IntPtr fileHandle);

    internal static void ThrowIfFailed(int hresult, string operation)
    {
        if (hresult < 0)
        {
            throw new COMException($"{operation} failed (HRESULT 0x{hresult:X8})", hresult);
        }
    }

    internal sealed class SafeCfHandle : SafeHandle
    {
        internal SafeCfHandle() : base(IntPtr.Zero, true) { }
        public override bool IsInvalid => handle == IntPtr.Zero || handle == new IntPtr(-1);
        protected override bool ReleaseHandle()
        {
            CfCloseHandle(handle);
            return true;
        }
    }
}
