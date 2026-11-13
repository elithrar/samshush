"""Read-only HDMI mailbox hosted by the existing Home worker."""

SOURCE = r"""
internal static partial class ArtQuietHours
{
    const string SourceLibrary = "/usr/lib/libsource-api.so";
    [DllImport(SourceLibrary)] static extern IntPtr get_source_list();
    [DllImport("libc.so.6")] static extern UIntPtr strnlen(IntPtr value, UIntPtr maximum);
    [DllImport("libc.so.6")] static extern void free(IntPtr value);

    public record HomeSource(int port, bool connected, string name, string detectedName);

    public static HomeSource[] ParseSources(string json)
    {
        using var document = JsonDocument.Parse(json, new JsonDocumentOptions { MaxDepth = 16 });
        var root = document.RootElement;
        if (root.ValueKind != JsonValueKind.Array || root.GetArrayLength() > 256)
            throw new InvalidOperationException("Invalid source inventory");
        var sources = new List<HomeSource>();
        foreach (var item in root.EnumerateArray()) {
            if (item.ValueKind != JsonValueKind.Object ||
                item.EnumerateObject().GroupBy(p => p.Name).Any(g => g.Count() != 1))
                throw new InvalidOperationException("Invalid source record");
            int type = item.GetProperty("sourceType").GetInt32();
            if (type < 13 || type > 16) continue;
            string Name(string key) {
                string raw = item.GetProperty(key).GetString() ?? "";
                if (raw.Length > 1024) throw new InvalidOperationException("Oversized source name");
                string value = Uri.UnescapeDataString(raw).Trim();
                if (value.Length > 128 || value.Any(char.IsControl))
                    throw new InvalidOperationException("Invalid source name");
                return value;
            }
            sources.Add(new HomeSource(type - 12, item.GetProperty("connectionState").GetBoolean(),
                Name("editName"), Name("detectedName")));
        }
        // Multiple activities on one port do not establish one physical device.
        return sources.GroupBy(s => s.port).Where(g => g.Count() == 1).Select(g => g.Single()).ToArray();
    }

    public static bool SourceToken(string value) => value.Length == 32 &&
        value.All(c => c is >= '0' and <= '9' or >= 'a' and <= 'f');

    static string CecQuery(string method, string argument)
    {
        var start = new ProcessStartInfo("/usr/bin/dbus-send") {
            UseShellExecute = false, RedirectStandardOutput = true, RedirectStandardError = true
        };
        foreach (string arg in new[]{"--system", "--print-reply", "--reply-timeout=500",
            "--dest=org.tizen.tv.hdmicec", "/org/tizen/tv/hdmicec", "org.tizen.tv.HdmiCec." + method, argument})
            start.ArgumentList.Add(arg);
        using var process = Process.Start(start) ?? throw new IOException("Cannot read CEC metadata");
        // Both pipes are drained; the enclosing source child has a hard deadline.
        var output = process.StandardOutput.ReadToEndAsync();
        var errors = process.StandardError.ReadToEndAsync();
        if (!process.WaitForExit(1000)) { process.Kill(); throw new IOException("CEC read timed out"); }
        if (process.ExitCode != 0) throw new IOException("CEC read failed");
        string text = output.GetAwaiter().GetResult();
        errors.GetAwaiter().GetResult();
        if (text.Length > 4096) throw new IOException("Oversized CEC reply");
        return text;
    }

    public static string[] CecValues(string reply) => reply.Split('\n').Select(s => s.Trim())
        .Where(s => s.StartsWith("int32 ", StringComparison.Ordinal) || s.StartsWith("string ", StringComparison.Ordinal)).ToArray();

    static HomeSource[] WithCecNames(HomeSource[] sources)
    {
        try {
            var countReply = CecValues(CecQuery("GetDeviceCount", "boolean:true"));
            if (countReply.Length != 2 || countReply[0] != "int32 0" ||
                !countReply[1].StartsWith("int32 ") || !int.TryParse(countReply[1].Substring(6), out int count) ||
                count < 0 || count > 16) throw new IOException("Invalid CEC count");
            var names = new List<(int port, string name)>();
            for (int i = 0; i < count; i++) {
                string argument = "int32:" + i.ToString(CultureInfo.InvariantCulture);
                var portReply = CecValues(CecQuery("GetDevicePortNum", argument));
                var nameReply = CecValues(CecQuery("GetDeviceName", argument));
                if (portReply.Length != 2 || nameReply.Length != 2 || portReply[0] != "int32 0" ||
                    nameReply[0] != "int32 0" || !portReply[1].StartsWith("int32 ") ||
                    !nameReply[1].StartsWith("string ") || !int.TryParse(portReply[1].Substring(6), out int port))
                    throw new IOException("Invalid CEC device");
                string name = JsonSerializer.Deserialize<string>(nameReply[1].Substring(7)) ?? "";
                if (name.Length > 128 || name.Any(char.IsControl)) throw new IOException("Invalid CEC name");
                names.Add((port, name.Trim()));
            }
            var unique = names.GroupBy(n => n.port).Where(g => g.Count() == 1).ToDictionary(g => g.Key, g => g.Single().name);
            return sources.Select(s => s.connected && string.IsNullOrWhiteSpace(s.detectedName) && unique.TryGetValue(s.port, out var name)
                ? s with { detectedName = name } : s).ToArray();
        } catch (Exception e) when (e is IOException or System.ComponentModel.Win32Exception or JsonException) {
            Console.Error.WriteLine("HOME_SOURCES_CEC_UNAVAILABLE=" + e.Message);
            return sources;
        }
    }

    static SafeFileHandle HomeData()
    {
        using var home = OpenAt(AtCwd, "/opt/usr/home/owner", Directory);
        using var apps = OpenAt(Fd(home), "apps_rw", Directory);
        using var pkg = OpenAt(Fd(apps), "Gxqk7W2na5", Directory);
        var data = OpenAt(Fd(pkg), "data", Directory);
        if (Metadata(data).Uid == Owner) return data;
        data.Dispose();
        throw new IOException("Unexpected widget data owner");
    }

    static int ReplySources(string token)
    {
        using var deadline = new Timer(_ => Environment.Exit(124), null, 10000, Timeout.Infinite);
        using (var file = File.OpenRead(SourceLibrary)) {
            using var sha = SHA256.Create();
            string hash = Convert.ToHexString(sha.ComputeHash(file)).ToLowerInvariant();
            if (hash != "11f108c67632df08dc8fa474fb2fe941d45edc2d6bdc9a934ade853e57bd8636")
                throw new InvalidOperationException("Source library changed");
        }
        using var data = HomeData();
        using var request = Regular(data, "hdmi-sources.request", Owner, 33);
        if (ReadText(Pinned(request), 33) != token + "\n") return 0;
        using var reply = Regular(data, "hdmi-sources.reply", Owner, 8192);
        IntPtr pointer = get_source_list();
        if (pointer == IntPtr.Zero) throw new IOException("Empty native source inventory");
        string json;
        try {
            ulong size = strnlen(pointer, (UIntPtr)1048577).ToUInt64();
            if (size == 0 || size > 1048576) throw new IOException("Invalid source inventory size");
            byte[] bytes = new byte[(int)size];
            Marshal.Copy(pointer, bytes, 0, bytes.Length);
            json = Utf8.GetString(bytes);
        } finally { free(pointer); }
        string answer = JsonSerializer.Serialize(new { token, sources = WithCecNames(ParseSources(json)) });
        if (Utf8.GetByteCount(answer) > 8192) throw new IOException("Oversized source reply");
        Replace(data, "hdmi-sources.reply", answer, Owner, 8192);
        Console.WriteLine("HOME_SOURCES_OK");
        return 0;
    }

    sealed class SourcesBridge : IDisposable
    {
        Process? child;
        long started, nextRead;
        string? delivered, problem;
        public void Poll()
        {
            try {
                long now = Environment.TickCount64;
                if (child != null) {
                    if (!child.HasExited && now - started < 12000) return;
                    if (!child.HasExited) { child.Kill(); Console.Error.WriteLine("HOME_SOURCES_TIMEOUT"); }
                    else if (child.ExitCode != 0) Console.Error.WriteLine("HOME_SOURCES_FAILED=" + child.ExitCode);
                    child.Dispose(); child = null;
                }
                if (now < nextRead) return;
                using var data = HomeData();
                using var request = Regular(data, "hdmi-sources.request", Owner, 33);
                string message = ReadText(Pinned(request), 33);
                if (message == delivered || message.Length != 33 || message[32] != '\n' ||
                    !SourceToken(message.Substring(0, 32))) return;
                // A single bounded child keeps source-service stalls out of the Art policy loop.
                var start = new ProcessStartInfo("/usr/bin/dotnet") { UseShellExecute = false };
                foreach (string arg in new[]{ConfigRoot + "/ArtQuietHours.dll", "sources", message.Substring(0, 32)})
                    start.ArgumentList.Add(arg);
                nextRead = now + 15000;
                child = Process.Start(start) ?? throw new IOException("Cannot start source readout");
                started = now; delivered = message; problem = null;
            } catch (FileNotFoundException) { /* Home has not requested labels. */ }
            catch (Exception e) when (e is IOException or UnauthorizedAccessException or
                System.ComponentModel.Win32Exception or InvalidOperationException) {
                if (problem != e.Message) Console.Error.WriteLine("HOME_SOURCES_ERROR=" + e.Message);
                problem = e.Message;
            }
        }
        public void Dispose()
        {
            if (child == null) return;
            try { if (!child.HasExited) child.Kill(); }
            finally { child.Dispose(); child = null; }
        }
    }
}
"""
