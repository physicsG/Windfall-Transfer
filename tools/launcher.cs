// Starts the app with its bundled Python, elevated through launcher.manifest. The ONEFILE build embeds runtime\ and
// app\ and unpacks them once per version into Program Files: only administrators can write there, so unlike in %TEMP%
// nothing can swap the files before they run elevated.
using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Text;
using System.Windows.Forms;
#if ONEFILE
using System.IO.Compression;
#endif

[assembly: AssemblyTitle("Windfall Transfer")]
[assembly: AssemblyProduct("Windfall Transfer")]
[assembly: AssemblyDescription("Connect a Mac to this PC over a USB-C cable")]
// build.py generates the version attributes and, for ONEFILE, the Payload class.

static class Launcher
{
    [STAThread]
    static int Main(string[] args)
    {
        try
        {
#if ONEFILE
            if (args.Length == 2 && args[0] == "--extract-only")  // used by the build to check the payload
            {
                Unpack(args[1]);
                return 0;
            }
            return Start(Unpacked(), args);
#else
            return Start(AppDomain.CurrentDomain.BaseDirectory, args);
#endif
        }
        catch (Exception e)
        {
            MessageBox.Show("Windfall Transfer couldn't start: " + e.Message, "Windfall Transfer", MessageBoxButtons.OK,
                            MessageBoxIcon.Error);
            return 1;
        }
    }

    static int Start(string folder, string[] args)
    {
        string python = Path.Combine(folder, @"runtime\pythonw.exe");
        string script = Path.Combine(folder, @"app\Windfall Transfer.pyw");
        if (!File.Exists(python) || !File.Exists(script))
        {
            MessageBox.Show("Windfall Transfer is incomplete: keep the 'runtime' and 'app' folders next to " +
                            "Windfall Transfer.exe.", "Windfall Transfer", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
        StringBuilder arguments = new StringBuilder("\"" + script + "\"");
        foreach (string arg in args)
        {
            arguments.Append(" \"").Append(arg.Replace("\"", "\\\"")).Append('"');
        }
        ProcessStartInfo start = new ProcessStartInfo(python, arguments.ToString());
        start.UseShellExecute = false;
        start.WorkingDirectory = folder;
        start.EnvironmentVariables.Remove("TCL_LIBRARY");  // another Tcl on the PC must not replace the bundled one
        start.EnvironmentVariables.Remove("TK_LIBRARY");
#if ONEFILE
        start.EnvironmentVariables["WINDFALL_UNPACKED"] = folder;  // lets "Remove from this PC" delete it too
#endif
        Process.Start(start);
        return 0;
    }

#if ONEFILE
    static string Unpacked()
    {
        string programFiles = Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles);
        string home = Path.Combine(programFiles, "Windfall Transfer");
        string target = Path.Combine(home, "app-" + Payload.Id);
        if (!File.Exists(Path.Combine(target, ".complete")))
        {
            string staging = target + ".unpacking-" + Guid.NewGuid().ToString("N");
            Unpack(staging);
            try
            {
                if (Directory.Exists(target))
                {
                    Directory.Delete(target, true);  // left over from an interrupted unpack
                }
                Directory.Move(staging, target);
            }
            catch (IOException)
            {
                if (!File.Exists(Path.Combine(target, ".complete")))
                {
                    throw;
                }
                TryDelete(staging);  // another launch finished unpacking first
            }
        }
        foreach (string old in Directory.GetDirectories(home, "app-*"))
        {
            bool unpacking = Path.GetFileName(old).IndexOf('.') >= 0;  // another launch's "app-<id>.unpacking-..."
            if (!unpacking && !string.Equals(old, target, StringComparison.OrdinalIgnoreCase))
            {
                TryDelete(old);  // earlier versions; skipped while one is still running
            }
        }
        return target;
    }

    static void Unpack(string folder)
    {
        Directory.CreateDirectory(folder);
        using (Stream payload = Assembly.GetExecutingAssembly().GetManifestResourceStream("payload.zip"))
        using (ZipArchive archive = new ZipArchive(payload, ZipArchiveMode.Read))
        {
            archive.ExtractToDirectory(folder);
        }
        File.WriteAllText(Path.Combine(folder, ".complete"), Payload.Id);
    }

    static void TryDelete(string folder)
    {
        try
        {
            Directory.Delete(folder, true);
        }
        catch (IOException)
        {
        }
        catch (UnauthorizedAccessException)
        {
        }
    }
#endif
}
