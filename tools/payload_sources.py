"""Source inputs for the TV's .NET payloads; materialized only when rebuilding.

Root helpers: chris-ritsen/samsung-tv-root at
4ad2c4d3b0d0e0da4b5b7ebe4a787e94596f416b (Unlicense; vendor/LICENSE).
OwnerExec: local fixed-identity native package-command helper.
HomeSourceReadout: local fixed native source/feature getter; private output only.
"""

HOME_SOURCE_READOUT = r"""using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Threading;

// Fixed readout and Apple TV identity guard: no setters, app launches or CoBA initialization.
// Native source initialization opens a temporary D-Bus connection/subscription.
// Output includes unrelated source metadata and must remain in private reports.
internal static class HomeSourceReadout
{
    private const string Build = "T-RSMFAKUC-0090-REL-202607141953";
    private const string Feature = "com.samsung/featureconf/sourceservice.display_unplugged_source";
    private const int MaxJsonBytes = 1024 * 1024;

    [DllImport("libc.so.6", CallingConvention = CallingConvention.Cdecl)]
    private static extern uint geteuid();
    [DllImport("libc.so.6", CallingConvention = CallingConvention.Cdecl)]
    private static extern UIntPtr strnlen(IntPtr value, UIntPtr maximum);
    [DllImport("libc.so.6", CallingConvention = CallingConvention.Cdecl)]
    private static extern void free(IntPtr value);
    [DllImport("/usr/lib/libsource-api.so", CallingConvention = CallingConvention.Cdecl)]
    private static extern IntPtr get_source_list();
    // The native implementation writes a single byte through bool*, verified in ARM disassembly.
    [DllImport("/usr/lib/libcapi-system-info.so.0", CallingConvention = CallingConvention.Cdecl)]
    private static extern int system_info_get_custom_bool(
        [MarshalAs(UnmanagedType.LPUTF8Str)] string key, ref byte value);

    public static int Main(string[] arguments)
    {
        // Keep the native getter bounded even when the TV has no timeout utility.
        // Do not write from this callback: blocked output must not delay process exit.
        using var deadline = new Timer(_ => Environment.Exit(124), null, 25000, Timeout.Infinite);
        try
        {
            string? expectedAppleTv = null;
            if (arguments.Length != 0)
            {
                if (arguments.Length != 2 || arguments[0] != "--verify-apple-tv"
                    || arguments[1].Length > 64
                    || !Regex.IsMatch(arguments[1], @"\As/source\.layer\.hdmi@HDMI1(?:/[0-9]+)?\z",
                        RegexOptions.CultureInvariant))
                    throw new InvalidOperationException("Expected no arguments or --verify-apple-tv with an HDMI1 component ID");
                expectedAppleTv = arguments[1];
            }
            if (!File.ReadAllLines("/etc/os-release").Contains("BUILD_ID=" + Build))
                throw new InvalidOperationException("Unexpected firmware build");
            if (geteuid() != 0)
                throw new InvalidOperationException("Temporary root transport is required");
            string[] owner = File.ReadAllLines("/etc/passwd")
                .Where(line => line.StartsWith("owner:", StringComparison.Ordinal)).ToArray();
            if (owner.Length != 1 || owner[0].Split(':').Length < 7 || owner[0].Split(':')[2] != "5001")
                throw new InvalidOperationException("Unexpected owner identity");
            VerifyHash("/usr/lib/libsource-api.so",
                "11f108c67632df08dc8fa474fb2fe941d45edc2d6bdc9a934ade853e57bd8636");
            VerifyHash("/usr/lib/libcapi-system-info.so.0",
                "f2cc5bb172ba45d090f5b359f4bbfe7982e49f96991efddc2d2f8d86ffed6f39");

            byte featureByte = 255;
            int featureStatus = system_info_get_custom_bool(Feature, ref featureByte);
            bool featureValid = featureStatus == 0 && featureByte <= 1;
            string raw = ReadNativeSources();
            using JsonDocument document = JsonDocument.Parse(raw,
                new JsonDocumentOptions { MaxDepth = 16, AllowTrailingCommas = false });
            if (document.RootElement.ValueKind != JsonValueKind.Array
                || document.RootElement.GetArrayLength() == 0
                || document.RootElement.GetArrayLength() > 256)
                throw new InvalidOperationException("Source list must be a nonempty bounded JSON array");

            var hdmi = new List<object>();
            var appleTvSources = new List<string>();
            var errors = new List<string>();
            int row = 0;
            foreach (JsonElement item in document.RootElement.EnumerateArray())
            {
                row++;
                try
                {
                    if (item.ValueKind != JsonValueKind.Object)
                        throw new InvalidOperationException("source item is not an object");
                    if (item.EnumerateObject().GroupBy(p => p.Name, StringComparer.Ordinal).Any(g => g.Count() > 1))
                        throw new InvalidOperationException("duplicate source property");
                    int sourceType = item.GetProperty("sourceType").GetInt32();
                    string? port = sourceType switch
                    {
                        13 => "HDMI1", 14 => "HDMI2", 15 => "HDMI3", 16 => "HDMI4",
                        38 => "HDMI5", 39 => "HDMI6", 46 => "HDMI7", 47 => "HDMI8", _ => null
                    };
                    if (port == null) continue;
                    int activity = item.GetProperty("mbrActivityIndex").GetInt32();
                    bool connected = item.GetProperty("connectionState").GetBoolean();
                    string sourceId = port + (activity < 0 ? "" : "/" + activity.ToString(CultureInfo.InvariantCulture));
                    string componentId = "s/source.layer.hdmi@" + sourceId;
                    string editName = Uri.UnescapeDataString(RequiredString(item, "editName"));
                    string detectedName = Uri.UnescapeDataString(RequiredString(item, "detectedName"));
                    hdmi.Add(new
                    {
                        sourceType, port, mbrActivityIndex = activity, connectionState = connected,
                        sourceId, componentId, editName, detectedName,
                        uniqueKey = RequiredString(item, "uniqueKey"),
                        passesUnpluggedFilter = featureValid ? (bool?)(connected || featureByte == 1) : null
                    });
                    if (sourceType == 13 && connected
                        && detectedName.Equals("Apple TV", StringComparison.OrdinalIgnoreCase))
                        appleTvSources.Add(componentId);
                }
                catch (Exception error) when (error is InvalidOperationException || error is KeyNotFoundException || error is FormatException)
                {
                    errors.Add("row " + row.ToString(CultureInfo.InvariantCulture) + ": " + error.Message);
                }
            }
            bool complete = featureValid && errors.Count == 0;
            bool? appleTvVerified = null;
            if (expectedAppleTv != null)
            {
                appleTvVerified = complete && appleTvSources.Count == 1
                    && appleTvSources[0] == expectedAppleTv;
                if (appleTvVerified != true)
                {
                    errors.Add("Apple TV source identity does not uniquely match the expected connected HDMI1 component");
                    complete = false;
                }
            }
            Console.WriteLine(JsonSerializer.Serialize(new
            {
                schema = "samshush-home-source-readout-v1", build = Build, complete,
                appleTvVerification = new { expectedComponentId = expectedAppleTv, passed = appleTvVerified },
                feature = new { key = Feature, status = featureStatus, valid = featureValid,
                    value = featureValid ? (bool?)(featureByte == 1) : null },
                nativeSources = document.RootElement, hdmiSources = hdmi, errors
            }));
            return complete ? 0 : 1;
        }
        catch (Exception error)
        {
            Console.Error.WriteLine(JsonSerializer.Serialize(new
                { schema = "samshush-home-source-readout-v1", complete = false, error = error.GetType().Name + ": " + error.Message }));
            return 1;
        }
    }

    private static string RequiredString(JsonElement element, string key)
    {
        string result = element.GetProperty(key).GetString()
            ?? throw new InvalidOperationException(key + " is null");
        if (Encoding.UTF8.GetByteCount(result) > 65536)
            throw new InvalidOperationException(key + " is oversized");
        return result;
    }

    private static string ReadNativeSources()
    {
        IntPtr pointer = get_source_list();
        if (pointer == IntPtr.Zero)
            throw new InvalidOperationException("Native get_source_list returned null");
        // Native CSourceAPI::GetSourceList returns strdup storage, not a borrowed pointer.
        try
        {
            ulong length = strnlen(pointer, new UIntPtr(MaxJsonBytes + 1)).ToUInt64();
            if (length == 0 || length > MaxJsonBytes)
                throw new InvalidOperationException("Native source JSON is empty or oversized");
            var bytes = new byte[(int)length];
            Marshal.Copy(pointer, bytes, 0, bytes.Length);
            return new UTF8Encoding(false, true).GetString(bytes);
        }
        finally { free(pointer); }
    }

    private static void VerifyHash(string path, string expected)
    {
        using var stream = File.OpenRead(path);
        using var hash = SHA256.Create();
        string actual = Convert.ToHexString(hash.ComputeHash(stream)).ToLowerInvariant();
        if (actual != expected) throw new InvalidOperationException("Native baseline changed: " + path);
    }
}
"""

SOURCES = {
    "HomeSourceReadout.cs": HOME_SOURCE_READOUT,
    "HomeSourceReadout.csproj": r"""<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net6.0</TargetFramework>
    <OutputType>Exe</OutputType>
    <UseAppHost>false</UseAppHost>
    <ImplicitUsings>disable</ImplicitUsings>
    <Nullable>enable</Nullable>
    <Deterministic>true</Deterministic>
    <DebugType>none</DebugType>
    <RollForward>Major</RollForward>
  </PropertyGroup>
</Project>
""",
    "SamsungTvArchiveRoot.cs": r"""using System;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;

internal static class SamsungTvArchiveRoot
{
    private const string StagingRoot = "/home/owner/share/tmp/sdk_tools/";
    private const string VerificationKey = "/usr/share/sdbd/public.pem";
    private const string Passwd = "/etc/passwd";
    private const int CloneNewMountNamespace = 0x00020000;
    private const uint MountBind = 4096;
    private const uint MountRecursive = 16384;
    private const uint MountPrivate = 1U << 18;
    private const int UnmountDetach = 2;
    private const ulong RequiredCapabilities = (1UL << 6) | (1UL << 21);

    public static int Main(string[] arguments)
    {
        if (arguments.Length != 1 || !ValidPackage(arguments[0]))
        {
            Console.Error.WriteLine("usage: SamsungTvArchiveRoot.dll archive-root-HEX");
            return 2;
        }

        string package = arguments[0];
        string staging = StagingRoot + package;
        string logPath = staging + "/launcher.log";
        bool keyMounted = false;
        bool passwdMounted = false;
        try
        {
            ulong capabilities = ReadEffectiveCapabilities();
            if ((capabilities & RequiredCapabilities) != RequiredCapabilities)
            {
                throw new InvalidOperationException("CAP_SYS_ADMIN and CAP_SETGID are required");
            }
            if (unshare(CloneNewMountNamespace) != 0)
            {
                throw NativeError("unshare(CLONE_NEWNS)");
            }
            if (mount(null, "/", null,
                new UIntPtr(MountRecursive | MountPrivate), null) != 0)
            {
                throw NativeError("make root mount tree private");
            }
            if (mount(staging + "/public.pem", VerificationKey, null,
                new UIntPtr(MountBind), null) != 0)
            {
                throw NativeError("bind verification key");
            }
            keyMounted = true;
            if (mount(staging + "/passwd", Passwd, null,
                new UIntPtr(MountBind), null) != 0)
            {
                throw NativeError("bind passwd view");
            }
            passwdMounted = true;

            var start = new ProcessStartInfo("/usr/sbin/sdbd-tarlauncher");
            start.UseShellExecute = false;
            start.RedirectStandardOutput = true;
            start.RedirectStandardError = true;
            start.ArgumentList.Add("--package_name");
            start.ArgumentList.Add(package);
            start.ArgumentList.Add("-xzf");
            start.ArgumentList.Add(StagingRoot + "on-demand/" + package + ".tar.gz");
            start.ArgumentList.Add("--to-command=/bin/sh");
            using (Process process = Process.Start(start))
            {
                if (process == null)
                {
                    throw new InvalidOperationException("tarlauncher did not start");
                }
                var stdout = process.StandardOutput.ReadToEndAsync();
                var stderr = process.StandardError.ReadToEndAsync();
                process.WaitForExit();
                File.WriteAllText(logPath,
                    "exit=" + process.ExitCode.ToString(CultureInfo.InvariantCulture)
                    + "\nstdout:\n" + stdout.GetAwaiter().GetResult()
                    + "\nstderr:\n" + stderr.GetAwaiter().GetResult());
                return process.ExitCode;
            }
        }
        catch (Exception error)
        {
            string message = error.GetType().Name + ": " + error.Message;
            try { File.WriteAllText(logPath, message + "\n"); }
            catch (Exception) { }
            Console.Error.WriteLine(message);
            return 1;
        }
        finally
        {
            if (passwdMounted) umount2(Passwd, UnmountDetach);
            if (keyMounted) umount2(VerificationKey, UnmountDetach);
        }
    }

    private static bool ValidPackage(string package)
    {
        const string prefix = "archive-root-";
        if (!package.StartsWith(prefix, StringComparison.Ordinal)
            || package.Length != prefix.Length + 16)
        {
            return false;
        }
        for (int i = prefix.Length; i < package.Length; i++)
        {
            char c = package[i];
            if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f')))
            {
                return false;
            }
        }
        return true;
    }

    private static ulong ReadEffectiveCapabilities()
    {
        foreach (string line in File.ReadLines("/proc/self/status"))
        {
            if (line.StartsWith("CapEff:", StringComparison.Ordinal))
            {
                return ulong.Parse(line.Substring(7).Trim(),
                    NumberStyles.HexNumber, CultureInfo.InvariantCulture);
            }
        }
        throw new InvalidDataException("CapEff is missing from /proc/self/status");
    }

    private static Exception NativeError(string action)
    {
        return new InvalidOperationException(action + " failed, errno "
            + Marshal.GetLastWin32Error().ToString(CultureInfo.InvariantCulture));
    }

    [DllImport("libc", SetLastError = true)]
    private static extern int unshare(int flags);

    [DllImport("libc", SetLastError = true)]
    private static extern int mount(string source, string target, string filesystem,
        UIntPtr flags, string data);

    [DllImport("libc", SetLastError = true)]
    private static extern int umount2(string target, int flags);
}
""",
    "SamsungTvArchiveRoot.csproj": r"""<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net6.0</TargetFramework>
    <OutputType>Exe</OutputType>
    <AssemblyName>SamsungTvArchiveRoot</AssemblyName>
    <UseAppHost>false</UseAppHost>
    <ImplicitUsings>disable</ImplicitUsings>
    <Nullable>disable</Nullable>
    <EnableDefaultCompileItems>false</EnableDefaultCompileItems>
    <Deterministic>true</Deterministic>
    <DebugType>none</DebugType>
    <DebugSymbols>false</DebugSymbols>
  </PropertyGroup>
  <ItemGroup>
    <Compile Include="SamsungTvArchiveRoot.cs" />
  </ItemGroup>
</Project>
""",
    "SamsungTvRootAgent.cs": r"""using System;
using System.Diagnostics;
using System.IO;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Threading.Tasks;

internal static class SamsungTvRootAgent
{
    private const string ProtocolVersion = "SAMSUNG-TV-ROOT/1";
    private const string AllowedStagingRoot =
        "/home/owner/share/tmp/sdk_tools/";
    private const string RuntimeRoot = "/run/samsung-tv-root";
    private const int ConnectTimeoutMilliseconds = 5000;
    private const int MaximumLineBytes = 4 * 1024 * 1024;
    private const int MaximumCommandBytes = 64 * 1024;
    private const int MaximumFileBytes = 4 * 1024 * 1024;
    private const int MaximumCommandOutputBytes = 1024 * 1024;
    private const int MaximumCommandTimeoutMilliseconds = 300000;
    private const int OpenReadWrite = 2;
    private const int SignalKill = 9;
    private static string statusPath;

    public static int Main(string[] arguments)
    {
        byte[] secret = null;
        string runtimeDirectory = null;
        try
        {
            if (arguments.Length != 4)
            {
                throw new ArgumentException(
                    "usage: SamsungTvRootAgent.dll CALLBACK_IPV4 PORT TOKEN_FILE STAGING_DIRECTORY");
            }
            string stagingDirectory = ValidateStagingDirectory(arguments[3]);
            statusPath = Path.Combine(stagingDirectory, "root-agent.status");
            WriteStatus("starting");
            runtimeDirectory = PrepareRuntimeDirectory();
            int port;
            if (!int.TryParse(arguments[1], out port)
                || port < 1
                || port > 65535)
            {
                throw new ArgumentException("invalid callback port");
            }

            string tokenPath = Path.GetFullPath(arguments[2]);
            if (!tokenPath.StartsWith(stagingDirectory, StringComparison.Ordinal))
            {
                throw new ArgumentException(
                    "root-agent token is outside the staging directory");
            }
            secret = ReadAndRemoveSecret(tokenPath);
            if (secret.Length < 32)
            {
                throw new InvalidDataException(
                    "root-agent secret must contain at least 32 bytes");
            }

            int session = setsid();
            if (session < 0 && getsid(0) != getpid())
            {
                throw new InvalidOperationException(
                    "setsid failed with errno " + Marshal.GetLastWin32Error());
            }
            DetachStandardStreams();
            RunSession(
                arguments[0],
                port,
                secret,
                stagingDirectory,
                runtimeDirectory);
            WriteStatus("stopped");
            return 0;
        }
        catch (Exception exception)
        {
            WriteStatus(
                "failed "
                + exception.GetType().Name
                + ": "
                + exception.Message);
            return 1;
        }
        finally
        {
            DeleteRuntimeDirectory(runtimeDirectory);
            if (secret != null)
            {
                Zero(secret);
            }
        }
    }

    private static void RunSession(
        string host,
        int port,
        byte[] secret,
        string readableDirectory,
        string runtimeDirectory)
    {
        using (TcpClient client = new TcpClient())
        {
            Task connect = client.ConnectAsync(host, port);
            if (!connect.Wait(ConnectTimeoutMilliseconds))
            {
                throw new TimeoutException("root-agent callback timed out");
            }
            connect.GetAwaiter().GetResult();
            client.NoDelay = true;
            using (NetworkStream stream = client.GetStream())
            {
                string hello = ReadLine(stream);
                string[] helloFields = hello.Split('\t');
                if (helloFields.Length != 2
                    || helloFields[0] != ProtocolVersion)
                {
                    throw new InvalidDataException("invalid root-agent hello");
                }
                byte[] nonce = HexDecode(helloFields[1]);
                if (nonce.Length != 32)
                {
                    throw new InvalidDataException("invalid challenge length");
                }

                string authPayload = string.Join(
                    "\t",
                    "AUTH",
                    getpid().ToString(),
                    getuid().ToString(),
                    geteuid().ToString(),
                    getgid().ToString(),
                    getegid().ToString(),
                    ReadStatusValue("CapEff"),
                    Convert.ToBase64String(
                        Encoding.UTF8.GetBytes(ReadText("/proc/self/attr/current"))));
                WriteAuthenticated(stream, nonce, secret, authPayload);
                WriteStatus(
                    string.Format(
                        "connected pid={0} uid={1} gid={2}",
                        getpid(),
                        getuid(),
                        getgid()));

                long previousSequence = 0;
                while (true)
                {
                    string line = ReadLine(stream);
                    string payload = VerifyAuthenticated(line, nonce, secret);
                    string[] fields = payload.Split('\t');
                    if (fields.Length < 2)
                    {
                        throw new InvalidDataException("invalid command frame");
                    }
                    long sequence;
                    if (!long.TryParse(fields[1], out sequence)
                        || sequence <= previousSequence)
                    {
                        throw new InvalidDataException(
                            "root-agent command sequence is not increasing");
                    }
                    previousSequence = sequence;

                    if (fields[0] == "PING" && fields.Length == 2)
                    {
                        WriteAuthenticated(
                            stream,
                            nonce,
                            secret,
                            string.Join(
                                "\t",
                                "PONG",
                                fields[1],
                                getuid().ToString(),
                                geteuid().ToString(),
                                getgid().ToString(),
                                getegid().ToString()));
                        continue;
                    }
                    if (fields[0] == "SHUTDOWN" && fields.Length == 2)
                    {
                        WriteAuthenticated(
                            stream,
                            nonce,
                            secret,
                            "BYE\t" + fields[1]);
                        return;
                    }
                    if (fields[0] == "READFILE" && fields.Length == 3)
                    {
                        byte[] pathBytes = Convert.FromBase64String(fields[2]);
                        if (pathBytes.Length == 0 || pathBytes.Length > 4096)
                        {
                            throw new InvalidDataException(
                                "invalid READFILE path length");
                        }
                        string path = Path.GetFullPath(
                            Encoding.UTF8.GetString(pathBytes));
                        if (!IsAllowedFilePath(
                                path,
                                readableDirectory,
                                runtimeDirectory))
                        {
                            throw new InvalidDataException(
                                "READFILE path is outside the allowed directories");
                        }
                        FileInfo file = new FileInfo(path);
                        if (!file.Exists
                            || file.Length < 0
                            || file.Length > MaximumFileBytes)
                        {
                            throw new InvalidDataException(
                                "READFILE payload is missing or too large");
                        }
                        byte[] data = File.ReadAllBytes(path);
                        string digest;
                        using (SHA256 sha256 = SHA256.Create())
                        {
                            digest = HexEncode(sha256.ComputeHash(data));
                        }
                        WriteAuthenticated(
                            stream,
                            nonce,
                            secret,
                            string.Join(
                                "\t",
                                "FILE",
                                fields[1],
                                data.Length.ToString(),
                                digest));
                        stream.Write(data, 0, data.Length);
                        stream.Flush();
                        continue;
                    }
                    if (fields[0] == "WRITEFILE" && fields.Length == 5)
                    {
                        byte[] pathBytes = Convert.FromBase64String(fields[2]);
                        int length;
                        if (pathBytes.Length == 0
                            || pathBytes.Length > 4096
                            || !int.TryParse(fields[3], out length)
                            || length < 0
                            || length > MaximumFileBytes)
                        {
                            throw new InvalidDataException(
                                "invalid WRITEFILE metadata");
                        }
                        string path = Path.GetFullPath(
                            Encoding.UTF8.GetString(pathBytes));
                        if (!IsAllowedFilePath(
                                path,
                                readableDirectory,
                                runtimeDirectory))
                        {
                            throw new InvalidDataException(
                                "WRITEFILE path is outside the allowed directories");
                        }
                        byte[] data = ReadExact(stream, length);
                        string digest;
                        using (SHA256 sha256 = SHA256.Create())
                        {
                            digest = HexEncode(sha256.ComputeHash(data));
                        }
                        if (!string.Equals(
                                digest,
                                fields[4],
                                StringComparison.OrdinalIgnoreCase))
                        {
                            throw new InvalidDataException(
                                "WRITEFILE payload digest mismatch");
                        }
                        using (FileStream file = new FileStream(
                            path,
                            FileMode.CreateNew,
                            FileAccess.Write,
                            FileShare.None))
                        {
                            file.Write(data, 0, data.Length);
                            file.Flush();
                        }
                        if (chmod(path, 384) != 0)
                        {
                            File.Delete(path);
                            throw new IOException(
                                "WRITEFILE chmod failed with errno "
                                + Marshal.GetLastWin32Error());
                        }
                        WriteAuthenticated(
                            stream,
                            nonce,
                            secret,
                            string.Join(
                                "\t",
                                "WROTE",
                                fields[1],
                                length.ToString(),
                                digest));
                        continue;
                    }
                    if (fields[0] != "EXEC" || fields.Length != 4)
                    {
                        throw new InvalidDataException("unsupported command frame");
                    }

                    int timeout;
                    if (!int.TryParse(fields[2], out timeout)
                        || timeout < 100
                        || timeout > MaximumCommandTimeoutMilliseconds)
                    {
                        throw new InvalidDataException("invalid command timeout");
                    }
                    byte[] commandBytes = Convert.FromBase64String(fields[3]);
                    if (commandBytes.Length == 0
                        || commandBytes.Length > MaximumCommandBytes)
                    {
                        throw new InvalidDataException("invalid command length");
                    }
                    string command = Encoding.UTF8.GetString(commandBytes);
                    CommandResult result = Execute(command, timeout);
                    WriteAuthenticated(
                        stream,
                        nonce,
                        secret,
                        string.Join(
                            "\t",
                            "RESULT",
                            fields[1],
                            result.ExitCode.ToString(),
                            result.TimedOut ? "1" : "0",
                            Convert.ToBase64String(result.StandardOutput),
                            Convert.ToBase64String(result.StandardError)));
                }
            }
        }
    }

    private static CommandResult Execute(string command, int timeout)
    {
        ProcessStartInfo start = new ProcessStartInfo();
        start.FileName = "/usr/bin/setsid";
        start.Arguments = "/bin/sh -c " + QuoteArgument(command);
        start.UseShellExecute = false;
        start.CreateNoWindow = true;
        start.RedirectStandardOutput = true;
        start.RedirectStandardError = true;

        using (Process process = Process.Start(start))
        {
            if (process == null)
            {
                throw new InvalidOperationException("failed to start /bin/sh");
            }
            Task<CapturedOutput> output = CaptureOutput(
                process.StandardOutput.BaseStream);
            Task<CapturedOutput> error = CaptureOutput(
                process.StandardError.BaseStream);
            bool exited = process.WaitForExit(timeout);
            if (!exited)
            {
                int signalResult = kill(-process.Id, SignalKill);
                if (signalResult < 0)
                {
                    try
                    {
                        process.Kill();
                    }
                    catch
                    {
                    }
                }
                process.WaitForExit();
            }
            Task.WaitAll(output, error);
            byte[] standardError = error.Result.Data;
            if (output.Result.Truncated || error.Result.Truncated)
            {
                standardError = AppendOutputLimitNotice(
                    standardError,
                    output.Result.Truncated,
                    error.Result.Truncated);
            }
            return new CommandResult(
                exited ? process.ExitCode : 124,
                !exited,
                output.Result.Data,
                standardError);
        }
    }

    private static async Task<CapturedOutput> CaptureOutput(Stream stream)
    {
        byte[] chunk = new byte[8192];
        bool truncated = false;
        using (MemoryStream retained = new MemoryStream())
        {
            while (true)
            {
                int count = await stream.ReadAsync(chunk, 0, chunk.Length);
                if (count == 0)
                {
                    break;
                }
                int remaining = MaximumCommandOutputBytes - (int)retained.Length;
                if (remaining > 0)
                {
                    retained.Write(chunk, 0, Math.Min(count, remaining));
                }
                if (count > remaining)
                {
                    truncated = true;
                }
            }
            return new CapturedOutput(retained.ToArray(), truncated);
        }
    }

    private static byte[] AppendOutputLimitNotice(
        byte[] standardError,
        bool standardOutputTruncated,
        bool standardErrorTruncated)
    {
        string streams = standardOutputTruncated && standardErrorTruncated
            ? "stdout and stderr"
            : standardOutputTruncated ? "stdout" : "stderr";
        byte[] notice = Encoding.UTF8.GetBytes(
            "\n[samsung-tv-root: " + streams + " exceeded the 1 MiB capture limit]\n");
        byte[] result = new byte[standardError.Length + notice.Length];
        Buffer.BlockCopy(standardError, 0, result, 0, standardError.Length);
        Buffer.BlockCopy(notice, 0, result, standardError.Length, notice.Length);
        return result;
    }

    private static void WriteAuthenticated(
        Stream stream,
        byte[] nonce,
        byte[] secret,
        string payload)
    {
        string line = payload + "\t" + Authenticate(nonce, secret, payload);
        byte[] bytes = Encoding.UTF8.GetBytes(line + "\n");
        stream.Write(bytes, 0, bytes.Length);
        stream.Flush();
    }

    private static string VerifyAuthenticated(
        string line,
        byte[] nonce,
        byte[] secret)
    {
        int separator = line.LastIndexOf('\t');
        if (separator < 1)
        {
            throw new InvalidDataException("missing frame authenticator");
        }
        string payload = line.Substring(0, separator);
        byte[] observed = HexDecode(line.Substring(separator + 1));
        byte[] expected = HexDecode(Authenticate(nonce, secret, payload));
        if (observed.Length != expected.Length
            || !FixedTimeEquals(observed, expected))
        {
            throw new InvalidDataException("invalid frame authenticator");
        }
        return payload;
    }

    private static string Authenticate(
        byte[] nonce,
        byte[] secret,
        string payload)
    {
        byte[] payloadBytes = Encoding.UTF8.GetBytes(payload);
        byte[] message = new byte[nonce.Length + 1 + payloadBytes.Length];
        Buffer.BlockCopy(nonce, 0, message, 0, nonce.Length);
        Buffer.BlockCopy(payloadBytes, 0, message, nonce.Length + 1, payloadBytes.Length);
        using (HMACSHA256 hmac = new HMACSHA256(secret))
        {
            return HexEncode(hmac.ComputeHash(message));
        }
    }

    private static string ReadLine(Stream stream)
    {
        using (MemoryStream buffer = new MemoryStream())
        {
            while (buffer.Length <= MaximumLineBytes)
            {
                int value = stream.ReadByte();
                if (value < 0)
                {
                    throw new EndOfStreamException("root-agent peer disconnected");
                }
                if (value == '\n')
                {
                    return Encoding.UTF8.GetString(buffer.ToArray()).TrimEnd('\r');
                }
                buffer.WriteByte((byte)value);
            }
        }
        throw new InvalidDataException("root-agent frame exceeds size limit");
    }

    private static byte[] ReadExact(Stream stream, int length)
    {
        byte[] data = new byte[length];
        int offset = 0;
        while (offset < length)
        {
            int count = stream.Read(data, offset, length - offset);
            if (count <= 0)
            {
                throw new EndOfStreamException("root-agent payload was truncated");
            }
            offset += count;
        }
        return data;
    }

    private static byte[] ReadAndRemoveSecret(string path)
    {
        byte[] secret;
        try
        {
            secret = Convert.FromBase64String(File.ReadAllText(path).Trim());
        }
        finally
        {
            try
            {
                File.Delete(path);
            }
            catch
            {
            }
        }
        return secret;
    }

    private static void DetachStandardStreams()
    {
        int descriptor = open("/dev/null", OpenReadWrite);
        if (descriptor < 0)
        {
            throw new InvalidOperationException(
                "open /dev/null failed with errno "
                + Marshal.GetLastWin32Error());
        }
        try
        {
            for (int target = 0; target <= 2; target++)
            {
                if (dup2(descriptor, target) < 0)
                {
                    throw new InvalidOperationException(
                        "dup2 failed with errno "
                        + Marshal.GetLastWin32Error());
                }
            }
        }
        finally
        {
            if (descriptor > 2)
            {
                close(descriptor);
            }
        }
    }

    private static string ReadText(string path)
    {
        try
        {
            return File.ReadAllText(path).Trim();
        }
        catch (Exception exception)
        {
            return "unavailable:" + exception.GetType().Name;
        }
    }

    private static string ReadStatusValue(string key)
    {
        try
        {
            foreach (string line in File.ReadAllLines("/proc/self/status"))
            {
                if (line.StartsWith(key + ":", StringComparison.Ordinal))
                {
                    return line.Substring(key.Length + 1).Trim();
                }
            }
            return "missing";
        }
        catch (Exception exception)
        {
            return "unavailable:" + exception.GetType().Name;
        }
    }

    private static string QuoteArgument(string value)
    {
        return "\"" + value.Replace("\\", "\\\\").Replace("\"", "\\\"") + "\"";
    }

    private static string HexEncode(byte[] bytes)
    {
        StringBuilder output = new StringBuilder(bytes.Length * 2);
        foreach (byte value in bytes)
        {
            output.Append(value.ToString("x2"));
        }
        return output.ToString();
    }

    private static byte[] HexDecode(string value)
    {
        if ((value.Length & 1) != 0)
        {
            throw new InvalidDataException("invalid hexadecimal length");
        }
        byte[] output = new byte[value.Length / 2];
        for (int index = 0; index < output.Length; index++)
        {
            output[index] = Convert.ToByte(value.Substring(index * 2, 2), 16);
        }
        return output;
    }

    private static bool FixedTimeEquals(byte[] first, byte[] second)
    {
        int difference = first.Length ^ second.Length;
        int length = Math.Min(first.Length, second.Length);
        for (int index = 0; index < length; index++)
        {
            difference |= first[index] ^ second[index];
        }
        return difference == 0;
    }

    private static void Zero(byte[] bytes)
    {
        for (int index = 0; index < bytes.Length; index++)
        {
            bytes[index] = 0;
        }
    }

    private static void WriteStatus(string value)
    {
        if (string.IsNullOrEmpty(statusPath))
        {
            return;
        }
        try
        {
            File.WriteAllText(
                statusPath,
                DateTime.UtcNow.ToString("o") + " " + value + Environment.NewLine);
        }
        catch
        {
        }
    }

    private static string ValidateStagingDirectory(string value)
    {
        string path = Path.GetFullPath(value);
        if (!path.EndsWith(Path.DirectorySeparatorChar.ToString()))
        {
            path += Path.DirectorySeparatorChar;
        }
        if (!path.StartsWith(AllowedStagingRoot, StringComparison.Ordinal))
        {
            throw new ArgumentException(
                "root-agent staging directory is outside the allowed root");
        }
        return path;
    }

    private static string PrepareRuntimeDirectory()
    {
        Directory.CreateDirectory(RuntimeRoot);
        if (chmod(RuntimeRoot, 448) != 0)
        {
            throw new IOException(
                "runtime root chmod failed with errno "
                + Marshal.GetLastWin32Error());
        }
        string path = Path.Combine(RuntimeRoot, "agent-" + getpid());
        if (Directory.Exists(path))
        {
            Directory.Delete(path, true);
        }
        Directory.CreateDirectory(path);
        if (chmod(path, 448) != 0)
        {
            Directory.Delete(path, true);
            throw new IOException(
                "runtime directory chmod failed with errno "
                + Marshal.GetLastWin32Error());
        }
        return path + Path.DirectorySeparatorChar;
    }

    private static bool IsAllowedFilePath(
        string path,
        string stagingDirectory,
        string runtimeDirectory)
    {
        return path.StartsWith(stagingDirectory, StringComparison.Ordinal)
            || path.StartsWith(runtimeDirectory, StringComparison.Ordinal);
    }

    private static void DeleteRuntimeDirectory(string path)
    {
        if (string.IsNullOrEmpty(path))
        {
            return;
        }
        try
        {
            Directory.Delete(path, true);
        }
        catch
        {
        }
    }

    private sealed class CommandResult
    {
        internal readonly int ExitCode;
        internal readonly bool TimedOut;
        internal readonly byte[] StandardOutput;
        internal readonly byte[] StandardError;

        internal CommandResult(
            int exitCode,
            bool timedOut,
            byte[] standardOutput,
            byte[] standardError)
        {
            ExitCode = exitCode;
            TimedOut = timedOut;
            StandardOutput = standardOutput;
            StandardError = standardError;
        }
    }

    private sealed class CapturedOutput
    {
        internal readonly byte[] Data;
        internal readonly bool Truncated;

        internal CapturedOutput(byte[] data, bool truncated)
        {
            Data = data;
            Truncated = truncated;
        }
    }

    [DllImport("libc", SetLastError = true)]
    private static extern int setsid();

    [DllImport("libc", SetLastError = true)]
    private static extern int getsid(int processId);

    [DllImport("libc", SetLastError = true)]
    private static extern int open(string path, int flags);

    [DllImport("libc", SetLastError = true)]
    private static extern int dup2(int oldDescriptor, int newDescriptor);

    [DllImport("libc", SetLastError = true)]
    private static extern int close(int descriptor);

    [DllImport("libc", SetLastError = true)]
    private static extern int chmod(string path, int mode);

    [DllImport("libc", SetLastError = true)]
    private static extern int kill(int processId, int signalNumber);

    [DllImport("libc")]
    private static extern int getpid();

    [DllImport("libc")]
    private static extern uint getuid();

    [DllImport("libc")]
    private static extern uint geteuid();

    [DllImport("libc")]
    private static extern uint getgid();

    [DllImport("libc")]
    private static extern uint getegid();
}
""",
    "SamsungTvRootAgent.csproj": r"""<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net6.0</TargetFramework>
    <OutputType>Exe</OutputType>
    <AssemblyName>SamsungTvRootAgent</AssemblyName>
    <UseAppHost>false</UseAppHost>
    <ImplicitUsings>disable</ImplicitUsings>
    <Nullable>disable</Nullable>
    <EnableDefaultCompileItems>false</EnableDefaultCompileItems>
    <Deterministic>true</Deterministic>
    <DebugType>none</DebugType>
    <DebugSymbols>false</DebugSymbols>
  </PropertyGroup>
  <ItemGroup>
    <Compile Include="SamsungTvRootAgent.cs" />
  </ItemGroup>
</Project>
""",
    "OwnerExec.cs": r"""using System;
using System.Diagnostics;
using System.Runtime.InteropServices;
internal static class Program
{
    [DllImport("libc.so.6", SetLastError = true)] private static extern int setgroups(UIntPtr count, IntPtr groups);
    [DllImport("libc.so.6", SetLastError = true)] private static extern int setgid(uint gid);
    [DllImport("libc.so.6", SetLastError = true)] private static extern int setuid(uint uid);
    [DllImport("libc.so.6")] private static extern uint getuid();
    [DllImport("libc.so.6")] private static extern uint geteuid();
    [DllImport("libc.so.6")] private static extern uint getgid();
    private static void Check(int result, string operation)
    {
        if (result != 0) throw new InvalidOperationException(operation + " failed, errno=" + Marshal.GetLastWin32Error());
    }
    public static int Main(string[] args)
    {
        try
        {
            if (args.Length == 0 || (args[0] != "/usr/bin/id" && args[0] != "/usr/bin/pkginfo" && args[0] != "/usr/bin/pkgcmd"))
                throw new ArgumentException("Only identity and native package tools are allowed");
            if (geteuid() != 0) throw new InvalidOperationException("Root parent required");
            Check(setgroups(UIntPtr.Zero, IntPtr.Zero), "setgroups");
            Check(setgid(100), "setgid");
            Check(setuid(5001), "setuid");
            if (getuid() != 5001 || geteuid() != 5001 || getgid() != 100)
                throw new InvalidOperationException("Owner identity verification failed");
            var info = new ProcessStartInfo(args[0]) { UseShellExecute = false };
            info.Environment["HOME"] = "/opt/usr/home/owner";
            info.Environment["USER"] = "owner";
            info.Environment["LOGNAME"] = "owner";
            for (int i = 1; i < args.Length; i++) info.ArgumentList.Add(args[i]);
            using var process = Process.Start(info) ?? throw new InvalidOperationException("Failed to start native tool");
            process.WaitForExit();
            return process.ExitCode;
        }
        catch (Exception error) { Console.Error.WriteLine(error.Message); return 1; }
    }
}
""",
    "OwnerExec.csproj": r"""<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net6.0</TargetFramework>
    <OutputType>Exe</OutputType>
    <UseAppHost>false</UseAppHost>
    <ImplicitUsings>disable</ImplicitUsings>
    <Nullable>enable</Nullable>
    <Deterministic>true</Deterministic>
    <DebugType>none</DebugType>
  </PropertyGroup>
</Project>
""",
}
