namespace QuarkSync.Provider;

internal static class Program
{
    private static async Task<int> Main(string[] args)
    {
        ProviderLog.Initialize();
        try
        {
            string root = ParseRoot(args);
            if (args.Contains("--unregister", StringComparer.OrdinalIgnoreCase))
            {
                Interop.CfApi.ThrowIfFailed(
                    Interop.CfApi.CfUnregisterSyncRoot(Path.GetFullPath(root)),
                    "CfUnregisterSyncRoot");
                return 0;
            }
            await using SyncRootProvider provider = new(root);
            Console.CancelKeyPress += (_, eventArgs) =>
            {
                eventArgs.Cancel = true;
                Environment.Exit(0);
            };
            await provider.RunAsync();
            return 0;
        }
        catch (Exception exception)
        {
            ProviderLog.Error("Provider terminated", exception);
            Console.Error.WriteLine($"QuarkSync Provider: {exception.Message}");
            return 1;
        }
    }

    private static string ParseRoot(string[] args)
    {
        for (int index = 0; index < args.Length - 1; index++)
        {
            if (args[index] == "--root") return args[index + 1];
        }
        return Environment.GetEnvironmentVariable("QUARKSYNC_ROOT")
            ?? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), "QuarkSync");
    }
}
