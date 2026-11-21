using System;
using System.IO;
using Tizen.Applications;
using Tizen.Applications.DataControl;
using Tizen.Multimedia;
using Tizen.System;

internal static class PanelMicrophones
{
    internal const string Active = "active";
    internal const string Idle = "idle";
    internal const string Unknown = "unknown";

    internal static string Read() => Read(ReadFrameworkRoute(), ReadPanelEndpoints);

    internal static string Read(string framework, Func<string> readPanelEndpoints)
    {
        if (framework == Active) return Active;
        // A direct hardware client can use a microphone while the framework route is idle.
        var panel = readPanelEndpoints();
        return panel == Unknown ? framework : panel;
    }

    private static string ReadPanelEndpoints()
    {
        try
        {
            if (!Information.TryGetValue<string>("http://tizen.org/system/model_name", out var model) ||
                model != "QN65LS03FWFXZA") return Unknown;

            var inventory = ReadMetadata("/proc/asound/pcm", 8192);
            var first = NamedInput(inventory, "02-06", "mic0 cap mic0 capture codec-6")
                ? ClassifyStatus(ReadMetadata("/proc/asound/card2/pcm6c/sub0/status", 2048)) : Unknown;
            var second = NamedInput(inventory, "02-12", "mic1 cap mic1 capture codec-12")
                ? ClassifyStatus(ReadMetadata("/proc/asound/card2/pcm12c/sub0/status", 2048)) : Unknown;
            return Combine(first, second);
        }
        catch (Exception)
        {
            // A TV can omit the platform key or deny this application's access to driver metadata.
            return Unknown;
        }
    }

    private static string ReadFrameworkRoute()
    {
        try
        {
            var observed = false;
            var ambiguous = false;
            foreach (var device in AudioManager.GetConnectedDevices())
            {
                if (device.Type != AudioDeviceType.BuiltinMic) continue;
                if (device.IoDirection != AudioDeviceIoDirection.Input)
                {
                    ambiguous = true;
                    continue;
                }
                if (device.IsRunning) return Active;
                observed = true;
            }
            return observed && !ambiguous ? Idle : Unknown;
        }
        catch (Exception)
        {
            // Unsupported features and devices disappearing during enumeration give no current reading.
            return Unknown;
        }
    }

    private static string? ReadMetadata(string path, int limit)
    {
        try
        {
            using var reader = File.OpenText(path);
            var buffer = new char[limit + 1];
            var count = reader.ReadBlock(buffer, 0, buffer.Length);
            return count <= limit ? new string(buffer, 0, count) : null;
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or System.Security.SecurityException)
        {
            return null;
        }
    }

    internal static bool NamedInput(string? inventory, string address, string name)
    {
        if (inventory == null) return false;
        using var lines = new StringReader(inventory);
        var matched = false;
        while (lines.ReadLine() is string line)
        {
            var fields = line.Split(':', StringSplitOptions.TrimEntries);
            if (fields[0] != address) continue;
            if (matched || fields.Length != 4 || fields[1] != name || fields[2] != "" ||
                fields[3] != "capture 1") return false;
            matched = true;
        }
        return matched;
    }

    internal static string ClassifyStatus(string? status)
    {
        if (status == null) return Unknown;
        if (status.TrimEnd('\r', '\n') == "closed") return Idle;
        using var lines = new StringReader(status);
        return lines.ReadLine() == "state: RUNNING" ? Active : Unknown;
    }

    internal static string Combine(string first, string second)
    {
        if (first == Active || second == Active) return Active;
        return first == Idle && second == Idle ? Idle : Unknown;
    }
}

internal sealed class MicrophoneProvider : Provider
{
    internal MicrophoneProvider() : base("status") { }

    protected override MapGetResult OnMapGet(string key)
    {
        if (CurrentClientAppId != "Gxqk7W2na5.Shortcuts" || key != "recording")
            return new MapGetResult(Array.Empty<string>(), false);

        return new MapGetResult(new[] { PanelMicrophones.Read() }, true);
    }

    // The platform provider base requires these even for a read-only MAP export.
    protected override SelectResult OnSelect(string query, string where, string[] columns, int count, string order,
        int page, int rows) => new SelectResult(null, false);
    protected override InsertResult OnInsert(string query, Bundle data) => new InsertResult(0, false);
    protected override UpdateResult OnUpdate(string query, string where, Bundle data) => new UpdateResult(false);
    protected override DeleteResult OnDelete(string query, string where) => new DeleteResult(false);
}

internal sealed class MicrophoneStatusService : ServiceApplication
{
    private readonly MicrophoneProvider provider = new();

    protected override void OnCreate()
    {
        base.OnCreate();
        provider.Run();
    }

    protected override void OnTerminate()
    {
        provider.Dispose();
        base.OnTerminate();
    }

    private static void Main(string[] args)
    {
        using var service = new MicrophoneStatusService();
        service.Run(args);
    }
}
