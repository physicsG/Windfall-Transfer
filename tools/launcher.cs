// Connect App.exe: starts the app with the Python runtime bundled next to it. Windows asks for administrator
// rights before this runs (launcher.manifest), and the app inherits them. Built by tools/build.py.
using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Text;
using System.Windows.Forms;

[assembly: AssemblyTitle("Connect App")]
[assembly: AssemblyProduct("Connect App")]
[assembly: AssemblyDescription("Connect a Mac to this PC over a USB-C cable")]
[assembly: AssemblyVersion("1.0.0.0")]
[assembly: AssemblyFileVersion("1.0.0.0")]

static class Launcher
{
    [STAThread]
    static int Main(string[] args)
    {
        string folder = AppDomain.CurrentDomain.BaseDirectory;
        string python = Path.Combine(folder, @"runtime\pythonw.exe");
        string script = Path.Combine(folder, @"app\Connect App.pyw");
        if (!File.Exists(python) || !File.Exists(script))
        {
            MessageBox.Show("Connect App is incomplete: keep the 'runtime' and 'app' folders next to Connect App.exe.",
                            "Connect App", MessageBoxButtons.OK, MessageBoxIcon.Error);
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
        try
        {
            Process.Start(start);
            return 0;
        }
        catch (Exception e)
        {
            MessageBox.Show("Connect App couldn't start: " + e.Message, "Connect App", MessageBoxButtons.OK,
                            MessageBoxIcon.Error);
            return 1;
        }
    }
}
