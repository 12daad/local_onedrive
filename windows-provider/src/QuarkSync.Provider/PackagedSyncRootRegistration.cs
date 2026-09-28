using Windows.ApplicationModel;
using Windows.Storage;
using Windows.Storage.Provider;

namespace QuarkSync.Provider;

internal static class PackagedSyncRootRegistration
{
    internal static async Task<bool> TryRegisterAsync(string root)
    {
        Package package;
        try
        {
            package = Package.Current;
            _ = package.Id.FamilyName;
        }
        catch (InvalidOperationException)
        {
            return false;
        }

        StorageFolder folder = await StorageFolder.GetFolderFromPathAsync(root);
        StorageProviderSyncRootInfo info = new()
        {
            Id = $"{package.Id.FamilyName}!QuarkSync",
            Path = folder,
            DisplayNameResource = "QuarkSync",
            IconResource = $"{Environment.ProcessPath},0",
            HydrationPolicy = StorageProviderHydrationPolicy.Full,
            PopulationPolicy = StorageProviderPopulationPolicy.Full,
            InSyncPolicy = StorageProviderInSyncPolicy.FileCreationTime
                | StorageProviderInSyncPolicy.DirectoryCreationTime
                | StorageProviderInSyncPolicy.FileLastWriteTime
                | StorageProviderInSyncPolicy.DirectoryLastWriteTime,
            Version = "0.1.0",
        };
        StorageProviderSyncRootManager.Register(info);
        ProviderLog.Info($"Registered packaged Windows.Storage.Provider sync root {info.Id}");
        return true;
    }
}
