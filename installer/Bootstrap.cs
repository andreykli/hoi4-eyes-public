// Original HOI4-EYES / NightPulse, Apache-2.0. No installation on startup.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Reflection;
using System.Security.Cryptography;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Win32;

[assembly:System.Runtime.Versioning.TargetFramework(".NETFramework,Version=v4.8")]

class EyesSetup : Form {
    readonly string bundle = AppDomain.CurrentDomain.BaseDirectory;
    readonly string root = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"Programs","HOI4-EYES");
    readonly JavaScriptSerializer json = new JavaScriptSerializer { MaxJsonLength = 8*1024*1024 };
    Dictionary<string,object> plan;
    bool uninstall;
    ComboBox language = new ComboBox();
    Label explanation = new Label();
    CheckBox consent = new CheckBox();
    Button start = new Button();
    ProgressBar progress = new ProgressBar();
    bool busy;
    string Lang { get { return new [] {"en","ru","fr"}[Math.Max(0,language.SelectedIndex)]; } }
    string T(string en,string ru,string fr) { return Lang=="ru"?ru:Lang=="fr"?fr:en; }
    static string Hash(string file) { using(var h=SHA256.Create()) using(var s=File.OpenRead(file)) return BitConverter.ToString(h.ComputeHash(s)).Replace("-","").ToLowerInvariant(); }
    static Dictionary<string,object> D(object value) { return (Dictionary<string,object>)value; }
    static string S(object value) { return Convert.ToString(value); }
    static string IOPath(string value) {
        if(value.StartsWith(@"\\?\",StringComparison.Ordinal))return value;
        string path=Path.GetFullPath(value);
        return path.StartsWith(@"\\",StringComparison.Ordinal)?@"\\?\UNC\"+path.Substring(2):@"\\?\"+path;
    }
    string Child(string relative) {
        if(relative.Contains("\\") || relative.Contains(":") || relative.StartsWith("/") || relative.Contains("\n") || relative.Contains("\r")) throw new Exception("Unsafe path");
        foreach(string part in relative.Split('/')) if(part==".." || part=="." || part.Length==0) throw new Exception("Unsafe path");
        string file=Path.GetFullPath(Path.Combine(bundle,relative.Replace('/',Path.DirectorySeparatorChar)));
        if(!file.StartsWith(Path.GetFullPath(bundle),StringComparison.OrdinalIgnoreCase)) throw new Exception("Outside bundle");
        for(string p=file; !String.Equals(p.TrimEnd('\\'),bundle.TrimEnd('\\'),StringComparison.OrdinalIgnoreCase);p=Path.GetDirectoryName(p))
            if((File.GetAttributes(IOPath(p))&FileAttributes.ReparsePoint)!=0) throw new Exception("Reparse path");
        return IOPath(file);
    }
    void Verify() {
        string embedded;
        using(var reader=new StreamReader(Assembly.GetExecutingAssembly().GetManifestResourceStream("eyes.plan"))) embedded=reader.ReadToEnd();
        plan=D(json.DeserializeObject(embedded));
        if(S(plan["owner"])!="HOI4-EYES installer C v1" || Convert.ToInt32(plan["schema"])!=1) throw new Exception("Unknown plan");
        if(File.ReadAllText(Child("_eyes_setup/plan.json"))!=embedded) throw new Exception("Plan modified");
        var files=D(plan["files"]);
        foreach(var row in files) {
            var expected=D(row.Value);string path=Child(row.Key);
            if(new FileInfo(path).Length!=Convert.ToInt64(expected["bytes"]) || Hash(path)!=S(expected["sha256"])) throw new Exception("Bundle integrity failed");
        }
        var directories=new Stack<string>();directories.Push(IOPath(Path.Combine(bundle,"_eyes_setup")));
        while(directories.Count>0) foreach(string path in Directory.GetFileSystemEntries(directories.Pop())) {
            var attributes=File.GetAttributes(path);
            if((attributes&FileAttributes.ReparsePoint)!=0)throw new Exception("Reparse setup path");
            if((attributes&FileAttributes.Directory)!=0) { directories.Push(path);continue; }
            string relative=path.Substring(IOPath(bundle).Length).Replace('\\','/');
            if(relative!="_eyes_setup/plan.json" && !files.ContainsKey(relative)) throw new Exception("Unexpected setup file");
        }
    }
    static string Q(string value) { if(value.Contains("\"") || value.Contains("\n") || value.Contains("\r")) throw new Exception("Invalid argument");return "\""+value+"\""; }
    string FindPython() {
        var candidates=new List<string>();
        candidates.Add(Path.Combine(root,"runtime","Python314","python.exe"));
        candidates.Add(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"Programs","Python","Python314","python.exe"));
        foreach(var hive in new [] { Registry.CurrentUser, Registry.LocalMachine }) {
            using(var key=hive.OpenSubKey(@"SOFTWARE\Python\PythonCore\3.14\InstallPath"))
                if(key!=null && key.GetValue("")!=null) candidates.Add(Path.Combine(S(key.GetValue("")),"python.exe"));
        }
        foreach(string python in candidates) {
            if(!File.Exists(python))continue;
            var probe=new ProcessStartInfo(python,"-I -c \"import sys,tkinter,venv; assert sys.version_info[:2] == (3,14)\"") { UseShellExecute=false };
            using(var p=Process.Start(probe)) { if(p.WaitForExit(15000) && p.ExitCode==0)return python; }
        }
        return null;
    }
    void Labels() {
        explanation.Text=T("Install EYES from this Workshop folder. First, verify files and find Python 3.14 with Tkinter. If missing, the official Python installer (about 34 MB) requires your consent. Other components are chosen in the next screen. HOI4 will not start.",
            "Установка EYES из папки Workshop. Сначала проверяются файлы и Python 3.14 с Tkinter. Если Python отсутствует, официальный установщик (около 34 МБ) запускается только с вашего согласия. Остальные компоненты выбираются на следующем экране. HOI4 не запускается.",
            "Installation depuis Workshop. Vérification des fichiers et de Python 3.14 avec Tkinter. Si nécessaire, l’installateur officiel Python (environ 34 Mo) demande votre accord. Les autres composants sont choisis ensuite. HOI4 ne démarre pas.");
        consent.Text=T("Allow downloading and opening the official Python installer if necessary.","Разрешаю загрузить и открыть официальный установщик Python, если он необходим.","Autoriser le téléchargement et l’ouverture de l’installateur officiel Python si nécessaire.");
        start.Text=T("Check and continue","Проверить и продолжить","Vérifier et continuer");
    }
    async void Begin(object sender,EventArgs e) {
        if(busy)return;
        busy=true;start.Enabled=false;language.Enabled=false;
        try {
            if(Environment.OSVersion.Version.Major<10 || Environment.OSVersion.Version.Build<19045)
                throw new Exception(T("Windows 10 22H2 or newer is required.","Нужна Windows 10 22H2 или новее.","Windows 10 22H2 ou plus récent est requis."));
            Verify();string python=FindPython();
            if(python==null) {
                if(!consent.Checked)throw new Exception(T("Python is missing. Consent is required.","Python не найден. Требуется ваше согласие.","Python manque. Votre accord est nécessaire."));
                var source=D(D(plan["sources"])["python"]);
                if(!S(source["url"]).StartsWith("https://www.python.org/ftp/python/",StringComparison.Ordinal)) throw new Exception("Unapproved Python source");
                if(Directory.Exists(root) && !File.Exists(Path.Combine(root,"installer-owner.json")) && Directory.GetFileSystemEntries(root).Length!=0)throw new Exception("Unmanaged installation directory");
                Directory.CreateDirectory(root);
                string marker=Path.Combine(root,"installer-owner.json");
                if(File.Exists(marker) && S(D(json.DeserializeObject(File.ReadAllText(marker)))["owner"])!="HOI4-EYES installer C v1")throw new Exception("Foreign installation");
                if((File.GetAttributes(root)&FileAttributes.ReparsePoint)!=0)throw new Exception("Reparse install root");
                if(!File.Exists(marker))File.WriteAllText(marker,"{\"owner\":\"HOI4-EYES installer C v1\"}");
                var cache=Path.Combine(root,"downloads");Directory.CreateDirectory(cache);
                var file=Path.Combine(cache,S(source["sha256"])+".exe");
                if((File.GetAttributes(cache)&FileAttributes.ReparsePoint)!=0)throw new Exception("Reparse download path");
                foreach(string candidate in new []{file,file+".part"})
                    if(File.Exists(candidate) && (File.GetAttributes(candidate)&FileAttributes.ReparsePoint)!=0)throw new Exception("Reparse download file");
                if(!File.Exists(file) || Hash(file)!=S(source["sha256"])) {
                    ServicePointManager.SecurityProtocol=SecurityProtocolType.Tls12;
                    using(var client=new WebClient()) {
                        client.DownloadProgressChanged+=(s,args)=>progress.Value=args.ProgressPercentage;
                        await client.DownloadFileTaskAsync(new Uri(S(source["url"])),file+".part");
                    }
                    if(new FileInfo(file+".part").Length!=Convert.ToInt64(source["bytes"]) || Hash(file+".part")!=S(source["sha256"]))throw new Exception("Python integrity failed");
                    if(File.Exists(file))File.Delete(file);File.Move(file+".part",file);
                }
                string target=Path.Combine(root,"runtime","Python314");
                using(var p=Process.Start(new ProcessStartInfo(file,"InstallAllUsers=0 PrependPath=0 Include_launcher=0 Include_test=0 Include_tcltk=1 TargetDir="+Q(target)) { UseShellExecute=true })) await Task.Run(()=>p.WaitForExit());
                python=FindPython();if(python==null)throw new Exception("Python installation was cancelled or did not provide Tkinter.");
            }
            string pythonw=Path.Combine(Path.GetDirectoryName(python),"pythonw.exe");
            var launch=new ProcessStartInfo(File.Exists(pythonw)?pythonw:python,
                Q(Child("_eyes_setup/app/tools/installation_wizard.py"))+" --bundle "+Q(bundle.TrimEnd('\\'))+" --root "+Q(root)+" --language "+Lang+(uninstall?" --uninstall":"")) { UseShellExecute=false };
            launch.EnvironmentVariables["PYTHONDONTWRITEBYTECODE"]="1";
            launch.EnvironmentVariables["PYTHONNOUSERSITE"]="1";
            launch.EnvironmentVariables.Remove("PYTHONPATH");launch.EnvironmentVariables.Remove("PYTHONHOME");
            Process.Start(launch);busy=false;Close();
        } catch(Exception error) { MessageBox.Show(this,T("Setup stopped: ","Установка остановлена: ","Installation arrêtée : ")+error.Message,"HOI4-EYES"); }
        finally { busy=false;if(!IsDisposed) { start.Enabled=true;language.Enabled=true; } }
    }
    EyesSetup() {
        Text="HOI4-EYES — Workshop Setup";Size=new Size(740,390);MinimumSize=Size;StartPosition=FormStartPosition.CenterScreen;
        language.Items.AddRange(new object[]{"English","Русский","Français"});language.SelectedIndex=0;language.DropDownStyle=ComboBoxStyle.DropDownList;
        language.SetBounds(20,15,220,30);explanation.SetBounds(20,55,680,150);consent.SetBounds(20,205,680,45);start.SetBounds(20,275,240,40);progress.SetBounds(280,280,400,25);
        Controls.AddRange(new Control[]{language,explanation,consent,start,progress});language.SelectedIndexChanged+=(s,e)=>Labels();start.Click+=Begin;Labels();
        FormClosing+=(s,e)=>{if(busy && !Disposing && !IsDisposed && start.Enabled==false)e.Cancel=true;};
    }
    [STAThread] static int Main(string[] args) {
        // Application-local .NET behavior only; do not change Windows policy.
        AppContext.SetSwitch("Switch.System.IO.UseLegacyPathHandling",false);
        AppContext.SetSwitch("Switch.System.IO.BlockLongPaths",false);
        Application.EnableVisualStyles();var app=new EyesSetup();
        if(args.Length==1 && args[0]=="--verify-only") { try { app.Verify();return 0; } catch(Exception error) {
            using(var stderr=new StreamWriter(Console.OpenStandardError())) { stderr.WriteLine(error.GetType().Name+": "+error.Message); }
            return error is PathTooLongException?4:2;
        } }
        if(args.Length==1 && args[0]=="--uninstall")app.uninstall=true;
        if(!Environment.Is64BitOperatingSystem) { MessageBox.Show("Windows x64 is required.");return 3; }
        Application.Run(app);return 0;
    }
}
