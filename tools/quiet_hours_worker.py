"""Device worker for suppressing fullscreen Frame artwork during local quiet hours."""

NAME = "ArtQuietHours"
PROJECT = """<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>
<OutputType>Exe</OutputType><TargetFramework>net6.0</TargetFramework>
<ImplicitUsings>disable</ImplicitUsings><Nullable>enable</Nullable>
<Deterministic>true</Deterministic><RollForward>Major</RollForward>
<DebugType>none</DebugType><PathMap>$(MSBuildProjectDirectory)=/samshush/ArtQuietHours</PathMap>
</PropertyGroup></Project>
"""

SOURCE = r"""using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Threading;
using Microsoft.Win32.SafeHandles;

internal static class ArtQuietHours
{
    const string Build = "T-RSMFAKUC-0090-REL-202607141953";
    const string Deviced = "/usr/lib/libdeviced.so.1.0.3";
    const string Screen = "/usr/lib/libcapi-screen.so.0.1.0";
    const string Aul = "/usr/lib/libaul.so.0.79.3";
    const string Launcher = "/usr/bin/dotnet-launcher-inhouse";
    const string Art = "org.tizen.art-app";
    const string Image = "com.samsung.tv.contentimageapp";
    const string ArtDll = "/opt/usr/apps/org.tizen.art-app/bin/ArtApp.dll";
    const string ImageDll = "/usr/apps/com.samsung.tv.contentimageapp/bin/ContentImageApp.dll";
    const uint Owner = 5001;
    const int GraceMs = 25000;
    const int PollMs = 3000;
    const int ConfirmMs = 800;
    static readonly Encoding Utf8 = new UTF8Encoding(false, true);
    static readonly Dictionary<string, string> Hashes = new() {
        [Deviced] = "ea3ddbe08dbad2eb3d969d0df434be23afe69eb00921a7969191646e2d1bcb1a",
        [Screen] = "7c0494278451ec9941765d5b5e89d2cbb73a78251ea9d1696be77591b0ad8767",
        [Aul] = "7fa8b885ddad2eaf87c66b3876b6462fc5e036236d0a4a9e763e7fce0b33b6de",
        ["/usr/bin/screend"] = "4360ac590d55d0f2639e3c49ef1ca2249ef68df65f52a31eb451e51d6b1043d4",
        ["/usr/bin/deviced"] = "2f58aacf9bf073f27ab4f49a54f07abd2bf4536ada3f00dec90110a8e56f9cc2",
        [Launcher] = "3186666daae073ec8528ff277b71ea071f71e8a30fa58126b4db6494104e55cf"
    };
    [DllImport(Deviced)] static extern int device_power_get_ambient_mode_type();
    [DllImport(Deviced)] static extern int device_power_get_ambient_mode();
    [DllImport(Deviced)] static extern int device_power_get_state();
    [DllImport(Deviced)] static extern int device_power_request_poweroff(int reason);
    [DllImport(Screen)] static extern int screen_manager_get_ambient_screen_state(out int state);
    [DllImport(Aul)] static extern int aul_window_get_focused_pid(out int pid);
    [DllImport(Aul)] static extern int aul_app_get_appid_bypid_for_uid(int pid, [Out] byte[] value, int size, uint uid);
    [DllImport("libc.so.6")] static extern uint getuid();
    [DllImport("libc.so.6")] static extern uint geteuid();
    [DllImport("libc.so.6")] static extern IntPtr readlink(string path, [Out] byte[] value, UIntPtr size);
    [StructLayout(LayoutKind.Sequential)]
    struct Timespec { public IntPtr Sec, NSec; }
    [DllImport("libc.so.6")] static extern int clock_gettime(int clock, out Timespec time);

    public record Focus(int Pid, ulong StartTicks, uint Uid, string App, string Exe, string ArtDll);
    public record View(int ModeType, int ScreenRc, int ScreenState, int PowerState, int AmbientMode, Focus? Focus);

    public static bool InWindow(int minute, int start, int end) =>
        minute >= 0 && minute < 1440 && start != end &&
        (start < end ? minute >= start && minute < end : minute >= start || minute < end);

    public static string Classify(View view)
    {
        if (view.ScreenRc != 0 || view.ScreenState is < 0 or > 4 || view.ModeType < 0 ||
            view.PowerState is < 0 or > 4 || view.AmbientMode is < 0 or > 3) return "unknown";
        // ArtDataManagement.ArtScreenAPI and screend distinguish Frame type 4,
        // fullscreen/FullPower 2, browser 1, low power 3, and suspend 4.
        if (view.ModeType != 4 || view.ScreenState != 2 ||
            view.PowerState != 0 || view.AmbientMode != 1) return "other";
        var focus = view.Focus;
        if (focus == null || focus.Pid <= 1 || focus.StartTicks == 0 || focus.App.Length == 0)
            return "unknown";
        if (focus.App != Art && focus.App != Image) return "other";
        string expected = focus.App == Art ? ArtDll : ImageDll;
        return focus.Uid == Owner && focus.Exe == Launcher && focus.ArtDll == expected ? "art" : "unknown";
    }

    public sealed class Policy
    {
        Focus? accepted;
        long acceptedSuspendMs, retryAfterMs;
        public int LastRequestCode { get; private set; }

        public string Step(Func<bool> inWindow, Func<View> read, Action settle,
            Func<bool> stopped, Func<int> request, long elapsedMs, long suspendedMs,
            Func<long>? nowMs = null)
        {
            if (!inWindow()) {
                accepted = null; retryAfterMs = 0; return "outside";
            }
            var first = read();
            string state = Classify(first);
            if (state == "other") { accepted = null; retryAfterMs = 0; }
            if (state != "art") return state;
            // A suspended process may miss the intervening panel-off state.
            // BOOTTIME includes suspend and MONOTONIC excludes it.
            if (accepted == first.Focus && Math.Abs(suspendedMs - acceptedSuspendMs) < 1000) return "latched";
            if (elapsedMs < retryAfterMs) return "cooldown";
            settle();
            if (stopped()) return "stopped";
            nowMs ??= () => Environment.TickCount64;
            long recheckStarted = nowMs();
            var second = read();
            if (Classify(second) != "art" || second != first) return "changed";
            if (!inWindow()) return "outside";
            if (stopped()) return "stopped";
            if (nowMs() - recheckStarted > 500) return "stale";
            LastRequestCode = request();
            if (LastRequestCode != 0) { retryAfterMs = elapsedMs + 60000; return "failed"; }
            accepted = second.Focus; acceptedSuspendMs = suspendedMs; retryAfterMs = 0;
            return "requested";
        }
    }

    static string ReadText(string path, int maximum)
    {
        using var input = File.OpenRead(path);
        var bytes = new byte[maximum + 1];
        int count = 0;
        while (count < bytes.Length) {
            int n = input.Read(bytes, count, bytes.Length - count);
            if (n == 0) break;
            count += n;
        }
        if (count == 0 || count > maximum) throw new IOException("Invalid size: " + path);
        return Utf8.GetString(bytes, 0, count);
    }
    static ulong StartTicks(string proc)
    {
        string stat = ReadText(proc + "/stat", 8192);
        int index = stat.LastIndexOf(") ", StringComparison.Ordinal);
        if (index < 0) throw new IOException("Malformed process stat");
        var fields = stat.Substring(index + 2).Split(' ', StringSplitOptions.RemoveEmptyEntries);
        if (fields.Length <= 19 || fields[0] is "Z" or "X" ||
            !ulong.TryParse(fields[19], NumberStyles.None, CultureInfo.InvariantCulture, out ulong start) || start == 0)
            throw new IOException("Invalid process start");
        return start;
    }
    public static string AssemblyArgument(string app, string[] args)
    {
        string expected = app == Art ? ArtDll : app == Image ? ImageDll : "";
        // The in-house launcher repeats the ArtApp.dll path in argv[0] and later argv.
        return expected.Length != 0 && args.Contains(expected, StringComparer.Ordinal) ? expected : "";
    }
    static Focus? ReadFocus()
    {
        if (aul_window_get_focused_pid(out int pid) != 0 || pid <= 1) return null;
        try {
            string proc = "/proc/" + pid.ToString(CultureInfo.InvariantCulture);
            ulong start = StartTicks(proc);
            string? ids = ReadText(proc + "/status", 32768).Split('\n')
                .SingleOrDefault(line => line.StartsWith("Uid:", StringComparison.Ordinal));
            if (ids == null) return null;
            var fields = ids.Substring(4).Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries);
            if (fields.Length != 4 || fields.Distinct().Count() != 1 ||
                !uint.TryParse(fields[0], NumberStyles.None, CultureInfo.InvariantCulture, out uint uid)) return null;
            var buffer = new byte[512];
            if (aul_app_get_appid_bypid_for_uid(pid, buffer, buffer.Length, uid) != 0) return null;
            int length = Array.IndexOf(buffer, (byte)0);
            if (length <= 0 || length > 256) return null;
            string app = Utf8.GetString(buffer, 0, length);
            var link = new byte[4096];
            long n = readlink(proc + "/exe", link, new UIntPtr((uint)link.Length)).ToInt64();
            if (n <= 0 || n >= link.Length) return null;
            string exe = Utf8.GetString(link, 0, (int)n);
            string dll = "";
            if (uid == Owner && exe == Launcher && (app == Art || app == Image)) {
                var args = ReadText(proc + "/cmdline", 65536).Split('\0');
                dll = AssemblyArgument(app, args);
            }
            if (StartTicks(proc) != start) return null;
            if (dll.Length != 0 && (aul_window_get_focused_pid(out int again) != 0 || again != pid))
                return null;
            return new Focus(pid, start, uid, app, exe, dll);
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException or
            DecoderFallbackException or InvalidOperationException) { return null; }
    }
    static View Read()
    {
        // Bound the compositor's D-Bus and screend's IPC. Both are local to the TV.
        using var timeout = new Timer(_ => Environment.Exit(124), null, 15000, Timeout.Infinite);
        int power = device_power_get_state(), mode = device_power_get_ambient_mode();
        int type = device_power_get_ambient_mode_type();
        int rc = screen_manager_get_ambient_screen_state(out int state);
        return new View(type, rc, state, power, mode, ReadFocus());
    }
    static int Standby()
    {
        using var timeout = new Timer(_ => Environment.Exit(124), null, 15000, Timeout.Infinite);
        // Stock tvtimer's Art expiry supplies ART_APP_TO_SUSPEND | TO_SUSPEND.
        return device_power_request_poweroff(0x4000001a);
    }
    static long ClockMs(int clock)
    {
        if (clock_gettime(clock, out var ts) != 0) throw new IOException("Cannot read monotonic clock");
        return checked(ts.Sec.ToInt64() * 1000 + ts.NSec.ToInt64() / 1000000);
    }
    static int Minute(DateTimeOffset wall) => wall.Year >= 2025 ? wall.Hour * 60 + wall.Minute : -1;
    static int ArgumentMinute(string s)
    {
        if (!int.TryParse(s, NumberStyles.None, CultureInfo.InvariantCulture, out int minute) ||
            minute < 0 || minute >= 1440 || s != minute.ToString(CultureInfo.InvariantCulture))
            throw new ArgumentException("Expected a minute of day between 0 and 1439");
        return minute;
    }

    public record Hours(int Start, int End)
    {
        public static Hours Parse(string first, string second)
        {
            var value = new Hours(ArgumentMinute(first), ArgumentMinute(second));
            if (value.Start == value.End) throw new ArgumentException("Quiet hours require distinct times");
            return value;
        }
        public string Line => FormattableString.Invariant($"{Start} {End}\n");
    }

    public sealed class Schedule
    {
        public readonly string Session = Guid.NewGuid().ToString("N");
        public Hours Current { get; private set; }
        public Schedule(Hours initial) { Current = initial; }

        static bool IsToken(string value) => value.Length == 32 &&
            value.All(c => (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'));

        public string Answer(string message, Action<Hours> save, Func<Hours> readCommitted)
        {
            if (!message.EndsWith('\n')) throw new ArgumentException("Unterminated Home request");
            var parts = message[..^1].Split(' ');
            if (parts.Length < 2 || !IsToken(parts[1])) throw new ArgumentException("Invalid request token");
            string result = "ok";
            if (parts.Length == 7 && parts[0] == "set" && IsToken(parts[2])) {
                var previous = Hours.Parse(parts[3], parts[4]);
                var next = Hours.Parse(parts[5], parts[6]);
                if (parts[2] != Session || Current != previous) result = "conflict";
                else if (next != Current) {
                    try { save(next); Current = next; }
                    catch (Exception e) when (e is IOException or UnauthorizedAccessException) {
                        // A failed flush can happen before or after rename. Continue
                        // enforcing the actual readable schedule and decline this SET.
                        try { Current = readCommitted(); }
                        catch (Exception failure) { throw new InvalidOperationException("Cannot recover the committed schedule", failure); }
                        Console.Error.WriteLine("QUIET_HOURS_HOME_SAVE_FAILED=" + e.Message);
                        result = "conflict";
                    }
                }
            } else if (parts.Length != 2 || parts[0] != "get") {
                throw new ArgumentException("Invalid Home schedule request");
            }
            return parts[1] + " " + Session + " " + result + " " + Current.Line;
        }
    }

    // ARM EABI open flags differ from asm-generic. statx has a fixed Linux UAPI
    // layout. O_PATH lets us reject devices, pipes and symlinks before opening
    // a leaf for actual I/O, even when the widget renames it concurrently.
    const int AtCwd = -100, Directory = 1 << 14, NoFollow = 1 << 15,
        CloseExec = 1 << 19, PathOnly = 1 << 21, WriteNew = 1 | 64 | 128;
    const string ConfigRoot = "/opt/usr/share/samshush-quiet-hours-v1";
    const string Smack = "security.SMACK64";
    [StructLayout(LayoutKind.Explicit, Size = 256)]
    struct Statx {
        [FieldOffset(0)] public uint Mask;
        [FieldOffset(16)] public uint Links;
        [FieldOffset(20)] public uint Uid;
        [FieldOffset(28)] public ushort Mode;
        [FieldOffset(40)] public ulong Size;
    }
    [DllImport("libc.so.6", SetLastError = true)] static extern int openat(int directory, string name, int flags, uint mode);
    [DllImport("libc.so.6", SetLastError = true)] static extern int statx(int dir, string path, int flags, uint mask, out Statx value);
    [DllImport("libc.so.6", SetLastError = true)] static extern int renameat(int oldDir, string oldName, int newDir, string newName);
    [DllImport("libc.so.6", SetLastError = true)] static extern int unlinkat(int dir, string name, int flags);
    [DllImport("libc.so.6", SetLastError = true)] static extern int fsync(int fd);
    [DllImport("libc.so.6", SetLastError = true)] static extern int fchown(int fd, uint uid, uint gid);
    [DllImport("libc.so.6", SetLastError = true)] static extern IntPtr getxattr(string path, string name, [Out] byte[] data, UIntPtr size);
    [DllImport("libc.so.6", SetLastError = true)] static extern int fsetxattr(int fd, string name, byte[] data, UIntPtr size, int flags);

    static int Fd(SafeFileHandle file) => file.DangerousGetHandle().ToInt32();
    static string Pinned(SafeFileHandle file) => "/proc/self/fd/" + Fd(file).ToString(CultureInfo.InvariantCulture);
    static IOException Io(string action) => new IOException(action + " (errno " + Marshal.GetLastPInvokeError() + ")");
    static SafeFileHandle OpenAt(int directory, string name, int flags)
    {
        int fd = openat(directory, name, flags | NoFollow | CloseExec, 384); // 0600 for new files
        if (fd < 0) {
            if (Marshal.GetLastPInvokeError() == 2) throw new FileNotFoundException(name);
            throw Io("open " + name);
        }
        return new SafeFileHandle(new IntPtr(fd), true);
    }
    static Statx Metadata(SafeFileHandle file)
    {
        const uint fields = 0x20f; // TYPE, MODE, NLINK, UID, SIZE
        if (statx(Fd(file), "", 0x1000, fields, out var value) != 0) throw Io("statx");
        if ((value.Mask & fields) != fields) throw new IOException("Incomplete file identity");
        return value;
    }
    static SafeFileHandle Regular(SafeFileHandle directory, string name, uint owner, int limit)
    {
        var file = OpenAt(Fd(directory), name, PathOnly);
        try {
            var stat = Metadata(file);
            if ((stat.Mode & 0xf000) != 0x8000 || stat.Links != 1 || stat.Uid != owner ||
                stat.Size > (ulong)limit || (owner == 0 && (stat.Mode & 0xfff) != 384))
                throw new IOException("Unexpected file identity: " + name);
            return file;
        } catch { file.Dispose(); throw; }
    }
    static void UnlinkTemp(int dir, string name)
    {
        if (unlinkat(dir, name, 0) != 0 && Marshal.GetLastPInvokeError() != 2)
            throw Io("unlink temporary file");
    }
    static void Replace(SafeFileHandle dir, string name, string contents, uint owner, int limit)
    {
        using var template = Regular(dir, name, owner, limit);
        string temp = name + ".tmp";
        int parent = Fd(dir);
        UnlinkTemp(parent, temp);
        using var fresh = OpenAt(parent, temp, WriteNew);
        try {
            // Create a new inode for every reply. Never open an app-provided
            // object for writing, including a regular file moved in from elsewhere.
            using var output = new FileStream(fresh, FileAccess.Write);
            output.Write(Utf8.GetBytes(contents));
            byte[] label = new byte[256];
            long length = getxattr(Pinned(template), Smack, label, (UIntPtr)label.Length).ToInt64();
            if (length < 0) {
                if (Marshal.GetLastPInvokeError() != 61) throw Io("read SMACK label"); // ENODATA
            } else if (fsetxattr(Fd(fresh), Smack, label, (UIntPtr)(ulong)length, 0) != 0)
                throw Io("preserve SMACK label");
            if (owner != 0 && fchown(Fd(fresh), owner, uint.MaxValue) != 0) throw Io("own Home reply");
            output.Flush(true);
            if (renameat(parent, temp, parent, name) != 0) throw Io("replace " + name);
            if (owner == 0 && fsync(parent) != 0) throw Io("sync configuration directory");
        } finally { UnlinkTemp(parent, temp); }
    }

    sealed class HomeBridge
    {
        readonly Schedule schedule;
        string? answeredRequest, answer, delivered, problem;
        public Hours Current => schedule.Current;
        static SafeFileHandle Root()
        {
            var root = OpenAt(AtCwd, ConfigRoot, Directory);
            try {
                var stat = Metadata(root);
                if (stat.Uid != 0 || (stat.Mode & 0xfff) != 448) // 0700
                    throw new IOException("Unexpected quiet-hours directory");
                return root;
            } catch { root.Dispose(); throw; }
        }
        static Hours ReadConfig()
        {
            using var root = Root();
            using var file = Regular(root, "config", 0, 10);
            string text = ReadText(Pinned(file), 10);
            var parts = text.TrimEnd('\n').Split(' ');
            if (parts.Length != 2) throw new IOException("Malformed quiet-hours configuration");
            var hours = Hours.Parse(parts[0], parts[1]);
            if (hours.Line != text) throw new IOException("Malformed quiet-hours configuration");
            return hours;
        }
        public HomeBridge() { schedule = new Schedule(ReadConfig()); }
        static void Save(Hours hours)
        {
            using var root = Root();
            Replace(root, "config", hours.Line, 0, 10);
            if (ReadConfig() != hours) throw new IOException("Quiet-hours configuration readback failed");
        }
        public void Poll()
        {
            try {
                using var home = OpenAt(AtCwd, "/opt/usr/home/owner", Directory);
                using var apps = OpenAt(Fd(home), "apps_rw", Directory);
                using var pkg = OpenAt(Fd(apps), "Gxqk7W2na5", Directory);
                using var data = OpenAt(Fd(pkg), "data", Directory);
                if (Metadata(data).Uid != Owner) throw new IOException("Unexpected widget data owner");
                using var request = Regular(data, "quiet-hours.request", Owner, 128);
                string message = ReadText(Pinned(request), 128);
                if (message == delivered) return;
                if (message != answeredRequest) {
                    // Check both ends before applying anything. If reply delivery
                    // later fails, retry this cached answer without replaying SET.
                    using var reply = Regular(data, "quiet-hours.reply", Owner, 128);
                    answer = schedule.Answer(message, Save, ReadConfig);
                    answeredRequest = message;
                }
                Replace(data, "quiet-hours.reply", answer!, Owner, 128);
                delivered = message;
                problem = null;
            } catch (FileNotFoundException) { /* The optional launcher may be absent. */ }
            catch (Exception e) when (e is IOException or ArgumentException or UnauthorizedAccessException) {
                if (problem != e.Message) Console.Error.WriteLine("QUIET_HOURS_HOME_ERROR=" + e.Message);
                problem = e.Message;
            }
        }
    }
    static void CheckEnvironment()
    {
        if (IntPtr.Size != 4 || RuntimeInformation.ProcessArchitecture != Architecture.Arm ||
            getuid() != 0 || geteuid() != 0 || !ReadText("/etc/os-release", 16384).Split('\n').Contains("BUILD_ID=" + Build))
            throw new InvalidOperationException("Unexpected build, architecture or privilege");
        var owners = ReadText("/etc/passwd", 131072).Split('\n').Where(s => s.StartsWith("owner:", StringComparison.Ordinal)).ToArray();
        if (owners.Length != 1 || owners[0].Split(':').ElementAtOrDefault(2) != "5001")
            throw new InvalidOperationException("Unexpected owner identity");
        var mismatches = new List<string>();
        foreach (var file in Hashes) {
            using var input = File.OpenRead(file.Key);
            using var sha = SHA256.Create();
            string actual = Convert.ToHexString(sha.ComputeHash(input)).ToLowerInvariant();
            if (actual != file.Value) mismatches.Add(file.Key + " expected " + file.Value + " actual " + actual);
        }
        if (mismatches.Count != 0) throw new InvalidOperationException("Firmware hash mismatch: " + string.Join(", ", mismatches));
    }
    public static int Main(string[] args)
    {
        bool check = args.Length > 0 && args[0] == "check";
        try {
            bool installed = args.Length == 1 && args[0] == "run";
            if (!installed && (args.Length != 3 || (!check && args[0] != "run")))
                throw new ArgumentException("Expected run, or check/run with start and end minutes");
            CheckEnvironment();
            var home = installed ? new HomeBridge() : null;
            var hours = home?.Current ?? Hours.Parse(args[1], args[2]);
            if (check) {
                long started = Environment.TickCount64;
                var view = Read(); var now = DateTimeOffset.Now;
                long sampleMs = Environment.TickCount64 - started;
                int minute = Minute(now); string state = Classify(view);
                bool complete = minute >= 0 && state != "unknown";
                Console.WriteLine("QUIET_HOURS_CHECK=" + JsonSerializer.Serialize(new {
                    schema = 1, complete, start = hours.Start, end = hours.End, wallClock = now, zone = TimeZoneInfo.Local.Id,
                    minute, inWindow = InWindow(minute, hours.Start, hours.End), artFullscreen = state == "art",
                    classification = state, sampleMs, view
                }));
                return complete ? 0 : 1;
            }
            using var stop = new ManualResetEvent(false);
            using var signal = PosixSignalRegistration.Create(PosixSignal.SIGTERM, ctx => { ctx.Cancel = true; stop.Set(); });
            var policy = new Policy();
            bool WindowOpen() {
                home?.Poll();
                var current = home?.Current ?? hours;
                return InWindow(Minute(DateTimeOffset.Now), current.Start, current.End);
            }
            Console.WriteLine("QUIET_HOURS_READY");
            long graceEnds = Environment.TickCount64 + GraceMs;
            while (Environment.TickCount64 < graceEnds) {
                home?.Poll(); // Answer Home while retaining the initial standby grace period.
                if (stop.WaitOne((int)Math.Clamp(graceEnds - Environment.TickCount64, 0, PollMs))) return 0;
            }
            while (!stop.WaitOne(0)) {
                long nowMs = ClockMs(1); // CLOCK_MONOTONIC
                long suspendMs = ClockMs(7) - nowMs; // CLOCK_BOOTTIME
                string outcome = policy.Step(WindowOpen, Read,
                    () => stop.WaitOne(ConfirmMs), () => stop.WaitOne(0), Standby, nowMs, suspendMs);
                if (outcome == "requested") Console.WriteLine("QUIET_HOURS_STANDBY_REQUESTED");
                if (outcome == "failed") Console.Error.WriteLine("QUIET_HOURS_STANDBY_FAILED=" + policy.LastRequestCode);
                if (stop.WaitOne(home == null && outcome == "outside" ? 15000 : PollMs)) break;
            }
            return 0;
        } catch (Exception e) {
            if (check) Console.WriteLine("QUIET_HOURS_CHECK=" + JsonSerializer.Serialize(new { schema = 1, complete = false, error = e.Message }));
            else Console.Error.WriteLine("QUIET_HOURS_ERROR=" + e.Message);
            return 1;
        }
    }
}
"""
