// VibeClipper.exe: abre o VibeClipper com dois cliques.
//   - se o editor já está rodando (porta 8765), só abre o navegador;
//   - senão, sobe "python editor.py" escondido (sem janela preta) e abre o navegador quando ele responder.
//   VibeClipper.exe fechar  -> fecha o editor que está rodando.
// O log fica em lancador\editor.log. Compilado com o csc.exe do .NET Framework (vem no Windows):
//   C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe /target:winexe /win32icon:vibeclipper.ico
//       /out:..\VibeClipper.exe /r:System.Windows.Forms.dll VibeClipper.cs
using System;
using System.Diagnostics;
using System.IO;
using System.Net.Sockets;
using System.Threading;
using System.Windows.Forms;

class VibeClipper
{
    const int PORTA = 8765;
    static readonly string URL = "http://127.0.0.1:" + PORTA + "/";

    static string Pasta()
    {
        string aqui = AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\');
        foreach (string p in new[] { aqui, Path.GetDirectoryName(aqui),
                 Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".claude\\skills\\vibeclipper") })
            if (!string.IsNullOrEmpty(p) && File.Exists(Path.Combine(p, "editor.py"))) return p;
        return null;
    }

    static bool Rodando()
    {
        try { using (var c = new TcpClient()) { var r = c.BeginConnect("127.0.0.1", PORTA, null, null);
              return r.AsyncWaitHandle.WaitOne(400) && c.Connected; } }
        catch { return false; }
    }

    static void AbrirNavegador() { Process.Start(new ProcessStartInfo(URL) { UseShellExecute = true }); }

    static void Fechar(string pasta)
    {
        string pidArq = Path.Combine(pasta, "lancador", "editor.pid");
        if (File.Exists(pidArq))
        {
            Process.Start(new ProcessStartInfo("taskkill", "/PID " + File.ReadAllText(pidArq).Trim() + " /T /F")
                          { CreateNoWindow = true, UseShellExecute = false }).WaitForExit();
            File.Delete(pidArq);
        }
        if (Rodando())   // aberto de outro jeito (Abrir editor.bat, terminal): acha quem escuta na porta
        {
            var p = new Process { StartInfo = new ProcessStartInfo("cmd.exe",
                "/c for /f \"tokens=5\" %a in ('netstat -ano ^| findstr \"127.0.0.1:" + PORTA + " \" ^| findstr LISTENING') do taskkill /PID %a /T /F")
                { CreateNoWindow = true, UseShellExecute = false } };
            p.Start(); p.WaitForExit();
        }
        MessageBox.Show("O VibeClipper foi fechado.", "VibeClipper", MessageBoxButtons.OK, MessageBoxIcon.Information);
    }

    [STAThread]
    static void Main(string[] args)
    {
        string pasta = Pasta();
        if (pasta == null)
        {
            MessageBox.Show("Não achei a pasta do VibeClipper (editor.py). Deixe o VibeClipper.exe dentro da pasta dele.",
                            "VibeClipper", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return;
        }
        if (args.Length > 0 && args[0].ToLower().Contains("fechar")) { Fechar(pasta); return; }
        if (Rodando()) { AbrirNavegador(); return; }

        string venv = Path.Combine(pasta, ".venv\\Scripts\\python.exe");
        string py = File.Exists(venv) ? "\"" + venv + "\"" : "python";
        string log = Path.Combine(pasta, "lancador", "editor.log");
        Directory.CreateDirectory(Path.GetDirectoryName(log));
        // cmd escondido: o python e o ffmpeg herdam o console escondido (nenhuma janela preta aparece)
        var si = new ProcessStartInfo("cmd.exe", "/c " + py + " editor.py --sem-navegador > \"" + log + "\" 2>&1")
        { WorkingDirectory = pasta, CreateNoWindow = true, UseShellExecute = false, WindowStyle = ProcessWindowStyle.Hidden };
        si.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
        si.EnvironmentVariables["PYTHONUTF8"] = "1";
        Process proc;
        try { proc = Process.Start(si); }
        catch (Exception e)
        {
            MessageBox.Show("Não consegui iniciar o Python: " + e.Message, "VibeClipper", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return;
        }
        File.WriteAllText(Path.Combine(pasta, "lancador", "editor.pid"), proc.Id.ToString());
        for (int i = 0; i < 120; i++)          // espera até 60 s o editor responder
        {
            if (Rodando()) { AbrirNavegador(); return; }
            if (proc.HasExited) break;
            Thread.Sleep(500);
        }
        string fim = "";
        try { var l = File.ReadAllLines(log); fim = string.Join("\n", l, Math.Max(0, l.Length - 12), Math.Min(12, l.Length)); } catch { }
        MessageBox.Show("O editor não abriu.\n\n" + fim + "\n\nLog completo: " + log, "VibeClipper",
                        MessageBoxButtons.OK, MessageBoxIcon.Error);
    }
}
