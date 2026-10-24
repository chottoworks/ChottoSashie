use std::io::{BufRead, BufReader, Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;

use tauri::{Emitter, Manager, WebviewUrl, WebviewWindowBuilder, WindowEvent};
use tauri_plugin_dialog::DialogExt;
use uuid::Uuid;

/// まず使いたい番号。開発時の手順もこの番号を前提にしている。
const PREFERRED_API_PORT: u16 = 8766;
/// 埋まっていたときに、ここまで順に空きを探す。
const API_PORT_ATTEMPTS: u16 = 20;

struct ApiProcess {
    child: Mutex<Option<Child>>,
    // このアプリがAPIを起動した場合だけ発行する保護トークン。
    token: Option<String>,
    // 実際に使っている番号（好みの番号が埋まっていればずれる）。
    port: u16,
}

fn api_is_ready(port: u16) -> bool {
    let address = SocketAddr::from(([127, 0, 0, 1], port));
    TcpStream::connect_timeout(&address, Duration::from_millis(300)).is_ok()
}

/// このアプリが使うべきデータ保存先。相乗り判定も、APIへ渡す値も、ここを見る。
fn expected_data_dir() -> Option<String> {
    let value = current_locations().data_dir;
    (!value.is_empty()).then_some(value)
}

/// 保存先の見た目の違いを揃えてから比べる。
///
/// Windowsは `/` も区切りとして受け付けるので、環境変数へ `D:/data` と書いてあっても
/// APIは `D:\data` と答える。区切りを揃えずに比べると、同じ場所を指しているのに
/// 「別の保存先」と誤って判定し、相乗りできるAPIを避けて二重に起動してしまう。
fn normalize_path(value: &str) -> String {
    value
        .trim()
        .replace('/', "\\")
        .trim_end_matches('\\')
        .to_lowercase()
}

/// ポートに居るAPIの `/health` を読む。読めなければ None。
fn probe_health(port: u16) -> Option<String> {
    let address = SocketAddr::from(([127, 0, 0, 1], port));
    let mut stream = TcpStream::connect_timeout(&address, Duration::from_millis(500)).ok()?;
    let _ = stream.set_read_timeout(Some(Duration::from_secs(5)));
    let request = format!(
        "GET /health HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n"
    );
    stream.write_all(request.as_bytes()).ok()?;
    let mut response = String::new();
    stream.read_to_string(&mut response).ok()?;
    Some(response)
}

/// その番号を誰が使っているか。
enum PortUse {
    /// 空いている。ここで起動してよい。
    Free,
    /// このアプリのAPIが、同じ保存先で動いている。相乗りしてよい。
    Ours,
    /// 別のプログラム、または別の保存先を使うちょっと挿絵。避けて次を探す。
    Taken(String),
}

/// その番号に誰が居るのかを確かめる。
///
/// 開いているだけで繋ぐと、別のアプリや、別の保存先を使うAPIへ黙って繋がる。
/// 作品が見当たらない・保存先が食い違うという分かりにくい事故になるため、
/// 正体と保存先まで見る。
fn inspect_port(port: u16) -> PortUse {
    if !api_is_ready(port) {
        return PortUse::Free;
    }
    let Some(response) = probe_health(port) else {
        return PortUse::Taken(format!("{port}番は別のプログラムが使っています"));
    };
    if !response.contains("\"app\":\"chotto-sashie\"") {
        return PortUse::Taken(format!("{port}番は別のアプリが使っています"));
    }
    let Some(expected) = expected_data_dir() else {
        return PortUse::Ours;
    };
    // 保存先が違うAPIは、見えている作品が別物になる。相乗りしない。
    let marker = "\"data_dir\":\"";
    if let Some(start) = response.find(marker) {
        let rest = &response[start + marker.len()..];
        if let Some(end) = rest.find('"') {
            let reported = rest[..end].replace("\\\\", "\\");
            if normalize_path(&reported) != normalize_path(&expected) {
                return PortUse::Taken(format!(
                    "{port}番では、別の保存先を使う「ちょっと挿絵」が動いています（{reported}）"
                ));
            }
        }
    }
    PortUse::Ours
}

/// 使う番号を決める。空いていれば起動し、自分のAPIが居れば相乗りする。
///
/// 番号を1つに決め打ちすると、
/// たまたま同じ番号を使う別のプログラムがあるだけで、利用者はそれを探し出して
/// 止めるまでこのアプリを使えない。空きを順に探し、見つけた番号を画面側へ伝える。
fn choose_api_port() -> Result<(u16, bool), String> {
    let mut blocked: Vec<String> = Vec::new();
    for offset in 0..API_PORT_ATTEMPTS {
        let port = PREFERRED_API_PORT + offset;
        match inspect_port(port) {
            PortUse::Free => return Ok((port, false)),
            PortUse::Ours => return Ok((port, true)),
            PortUse::Taken(reason) => blocked.push(reason),
        }
    }
    let last = PREFERRED_API_PORT + API_PORT_ATTEMPTS - 1;
    Err(format!(
        "{PREFERRED_API_PORT}番から{last}番まで空きがありませんでした。{}",
        blocked.first().cloned().unwrap_or_default()
    ))
}

fn stop_managed_runtime_before_api_exit(port: u16, token: Option<&str>) {
    // APIを強制終了する前に、API自身が所有する専用ComfyUIだけを正常停止させる。
    let address = SocketAddr::from(([127, 0, 0, 1], port));
    let Ok(mut stream) = TcpStream::connect_timeout(&address, Duration::from_millis(500)) else {
        return;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_secs(15)));
    let token_header = match token {
        Some(value) => format!("X-Chotto-Sashie-Token: {value}\r\n"),
        None => String::new(),
    };
    let request = format!(
        "POST /api/v1/setup/runtime/stop HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Length: 0\r\n{token_header}Connection: close\r\n\r\n"
    );
    if stream.write_all(request.as_bytes()).is_ok() {
        let mut response = Vec::new();
        let _ = stream.read_to_end(&mut response);
    }
}

/// APIを動かすPythonと、そこへ渡す設定。
struct ApiRuntime {
    python: PathBuf,
    working_directory: PathBuf,
    /// 同梱物（ワークフロー・マニフェスト）の場所。
    /// 同梱していないとき（開発時）は None にして、API側の探索に任せる。
    resources: Option<PathBuf>,
    /// `-m uvicorn` の前に足す引数。開発時だけ、リポジトリのソースを読ませるために使う。
    extra_arguments: Vec<String>,
}

/// 配布版の一式（アプリに同梱したPythonと同梱物）。無ければ None。
///
/// 置き場所は `installer/build_api_runtime.py` が作り、`tauri.conf.json` の
/// `bundle.resources` でここへ運ばれる。
fn bundled_runtime(app: &tauri::AppHandle) -> Option<ApiRuntime> {
    bundled_runtime_at(app.path().resource_dir().ok()?)
}

fn bundled_runtime_at(resource_root: PathBuf) -> Option<ApiRuntime> {
    let runtime = resource_root.join("api-runtime");
    let python = runtime.join("python.exe");
    if !python.is_file() {
        return None;
    }
    Some(ApiRuntime {
        python,
        working_directory: runtime,
        resources: Some(resource_root.join("app")),
        extra_arguments: Vec::new(),
    })
}

/// 開発時の一式（リポジトリの `.venv` とソース）。無ければ None。
///
/// ビルド時のパスに頼るのはデバッグビルドだけにする。配布する実行ファイルへ
/// 焼き込むと、ビルドした本人の環境でしか動かないものが出来上がる。
#[cfg(debug_assertions)]
fn development_runtime() -> Option<ApiRuntime> {
    let repository_root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../..")
        .canonicalize()
        .ok()?;
    let python = repository_root.join(".venv/Scripts/python.exe");
    if !python.is_file() {
        return None;
    }
    let api_source = repository_root.join("services/api/src");
    Some(ApiRuntime {
        python,
        working_directory: repository_root,
        // リポジトリの中なので、API側が親をたどって同梱物を見つけられる。
        resources: None,
        extra_arguments: vec![
            "--app-dir".to_string(),
            api_source.to_string_lossy().into_owned(),
        ],
    })
}

#[cfg(not(debug_assertions))]
fn development_runtime() -> Option<ApiRuntime> {
    None
}

/// APIを動かす一式を選ぶ。開発時はリポジトリを先に見る。
///
/// デバッグビルドでは `target/debug` に前回運ばれた同梱物（`api-runtime` と `app`）が
/// 残っている。同梱版を先に見ると、リポジトリのソースやマニフェストを直しても、動くのは
/// その古い複製のままになる。配布版では `development_runtime` が常に None なので、
/// 残るのは同梱版だけで、ここでの順番は影響しない。
fn api_runtime(app: &tauri::AppHandle) -> Option<ApiRuntime> {
    development_runtime().or_else(|| bundled_runtime(app))
}

/// このプロセスが死んだら、起動した子も道連れにする仕組みを用意する。
///
/// 後片付けはウィンドウの破棄で行っているが、タスクマネージャー等で強制終了された
/// ときには走らない。実際にAPIのPythonが残り、ポートと保存先を掴んだままになった
/// （試験中に3つ残った）。OSに面倒を見てもらえば、どんな終わり方でも取りこぼさない。
///
/// ジョブへの参照が全て閉じた時点で中の処理が終わる。ここで作った参照はこのプロセスが
/// 持ち続け、プロセスが消えるときにOSが閉じる——だから明示的に閉じてはいけない。
/// 子がさらに起動したもの（専用ComfyUIなど）も同じジョブへ入るので、まとめて終わる。
#[cfg(windows)]
fn kill_on_close_job() -> Option<isize> {
    use std::sync::OnceLock;

    use windows_sys::Win32::Foundation::CloseHandle;
    use windows_sys::Win32::System::JobObjects::{
        CreateJobObjectW, JobObjectExtendedLimitInformation,
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
        SetInformationJobObject,
    };

    static JOB: OnceLock<isize> = OnceLock::new();
    let handle = *JOB.get_or_init(|| unsafe {
        let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
        if job.is_null() {
            return 0;
        }
        let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        let applied = SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            std::ptr::addr_of!(limits).cast(),
            std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        );
        if applied == 0 {
            CloseHandle(job);
            return 0;
        }
        job as isize
    });
    (handle != 0).then_some(handle)
}

/// 起動した子を、道連れの仕組みへ入れる。入れられなくても起動は続ける
/// （残る可能性が戻るだけで、入れられないこと自体は起動を止める理由にならない）。
#[cfg(windows)]
fn adopt_child(child: &Child) -> bool {
    use std::os::windows::io::AsRawHandle;

    use windows_sys::Win32::System::JobObjects::AssignProcessToJobObject;

    let Some(job) = kill_on_close_job() else {
        return false;
    };
    unsafe { AssignProcessToJobObject(job as _, child.as_raw_handle() as _) != 0 }
}

#[cfg(not(windows))]
fn adopt_child(_child: &Child) -> bool {
    false
}

/// このインストールが使う保存先と共有フォルダ。
///
/// 置き場所は**実行ファイルの隣**にする。保存先そのものへ置くと、保存先を変えた
/// 瞬間に設定が読めなくなる。実行ファイルの隣なら、インストールを分けたときに
/// それぞれが別の場所を持てる（同じ端末で用途別に使い分けられる）。
const LOCATIONS_FILE: &str = "paths.json";

#[derive(serde::Serialize, serde::Deserialize, Default)]
struct StoredLocations {
    #[serde(default)]
    data_dir: String,
    #[serde(default)]
    shared_dir: String,
}

/// 画面へ返す、いま使っている場所と、その決まり方。
#[derive(serde::Serialize)]
struct Locations {
    data_dir: String,
    shared_dir: String,
    /// environment | file | default。environment のときは画面から変えられない。
    data_dir_source: String,
    shared_dir_source: String,
    /// 設定ファイルの場所（読めない・書けないときの手がかりとして画面へ出す）。
    config_path: String,
}

fn locations_file() -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    Some(exe.parent()?.join(LOCATIONS_FILE))
}

/// 設定ファイルを読む。**壊れていたら黙って既定へ落とさない。**
///
/// 落とすと、別の場所（既定の保存先）を何事もなく開いてしまう。利用者から見れば
/// 作品が全部消えたのと区別が付かない。読めないなら読めないと言って止める。
fn stored_locations() -> Result<StoredLocations, String> {
    let Some(path) = locations_file() else {
        return Ok(StoredLocations::default());
    };
    let text = match std::fs::read_to_string(&path) {
        Ok(text) => text,
        // まだ無いのは正常（既定の場所を使う）。
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Ok(StoredLocations::default())
        }
        Err(error) => {
            return Err(format!("置き場所の設定を読めません（{}）: {error}", path.display()))
        }
    };
    serde_json::from_str(&text).map_err(|error| {
        format!(
            "置き場所の設定が壊れています（{}）: {error}。直すか消すかしてから、もう一度起動してください",
            path.display()
        )
    })
}

fn default_under_local_app_data(folder: &str) -> String {
    std::env::var("LOCALAPPDATA")
        .map(|root| format!("{}\\{folder}", root.trim_end_matches('\\')))
        .unwrap_or_default()
}

/// 1つぶんの場所を決める。環境変数 → 設定ファイル → 既定 の順。
///
/// 環境変数を最優先にするのは、開発と試験がそれで保存先を隔離しているため。
/// そこを設定ファイルで上書きすると、試験が実データへ書き込む事故が起きる。
fn resolve_location(variable: &str, folder: &str, stored: &str) -> (String, String) {
    if let Ok(value) = std::env::var(variable) {
        let trimmed = value.trim();
        if !trimmed.is_empty() && trimmed != "." {
            return (trimmed.to_string(), "environment".to_string());
        }
    }
    let stored = stored.trim();
    if !stored.is_empty() {
        return (stored.to_string(), "file".to_string());
    }
    (default_under_local_app_data(folder), "default".to_string())
}

fn current_locations() -> Locations {
    let stored = stored_locations().unwrap_or_default();
    let (data_dir, data_dir_source) =
        resolve_location("CHOTTO_SASHIE_DATA_DIR", "ChottoSashie", &stored.data_dir);
    let (shared_dir, shared_dir_source) = resolve_location(
        "CHOTTO_SASHIE_SHARED_DIR",
        "ChottoSashieShared",
        &stored.shared_dir,
    );
    Locations {
        data_dir,
        shared_dir,
        data_dir_source,
        shared_dir_source,
        config_path: locations_file()
            .map(|path| path.to_string_lossy().into_owned())
            .unwrap_or_default(),
    }
}

fn write_locations(data_dir: &str, shared_dir: &str) -> Result<(), String> {
    let path = locations_file().ok_or_else(|| "設定の置き場所を決められません".to_string())?;
    let stored = StoredLocations {
        data_dir: data_dir.trim().to_string(),
        shared_dir: shared_dir.trim().to_string(),
    };
    let text = serde_json::to_string_pretty(&stored)
        .map_err(|error| format!("設定を組み立てられません: {error}"))?;
    std::fs::write(&path, text)
        .map_err(|error| format!("設定を書けません（{}）: {error}", path.display()))
}

fn start_local_api(app: &tauri::AppHandle, token: &str) -> Result<(u16, Option<Child>), String> {
    // 置き場所が確かめられないうちは動かさない。別の場所を開くほうが害が大きい。
    stored_locations()?;
    let (port, reuse) = choose_api_port()?;
    // すでに自分のAPIが動いていれば相乗りする（開発時に手で起動している場合）。
    if reuse {
        return Ok((port, None));
    }

    let runtime =
        api_runtime(app).ok_or_else(|| "アプリ用Python環境が見つかりません".to_string())?;

    let locations = current_locations();
    let mut command = Command::new(&runtime.python);
    command
        .current_dir(&runtime.working_directory)
        .env("CHOTTO_SASHIE_API_TOKEN", token)
        // 決めた場所は子へ渡す。子に決め直させると、設定ファイルを読む側と読まない側で
        // 食い違い、画面が見ている保存先とAPIが書く保存先がずれる。
        .env("CHOTTO_SASHIE_DATA_DIR", &locations.data_dir)
        .env("CHOTTO_SASHIE_SHARED_DIR", &locations.shared_dir);
    if let Some(resources) = &runtime.resources {
        // 同梱物の場所は、探させるより教えるほうが確実。
        command.env("CHOTTO_SASHIE_RESOURCES_DIR", resources);
    }
    command
        .arg("-m")
        .arg("uvicorn")
        .arg("chotto_sashie_api.main:app")
        .args(&runtime.extra_arguments)
        .args(["--host", "127.0.0.1", "--port", &port.to_string()]);

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }

    let mut child = command
        .spawn()
        .map_err(|error| format!("ローカルAPIを起動できません: {error}"))?;
    if !adopt_child(&child) {
        // 起動は続ける。強制終了されたときに残りうる、というだけのため。
        eprintln!("[chotto-sashie] APIを道連れの仕組みへ入れられませんでした");
    }

    for _ in 0..100 {
        if api_is_ready(port) {
            return Ok((port, Some(child)));
        }
        if let Some(status) = child
            .try_wait()
            .map_err(|error| format!("ローカルAPIの状態を確認できません: {error}"))?
        {
            return Err(format!("ローカルAPIが起動直後に終了しました: {status}"));
        }
        std::thread::sleep(Duration::from_millis(50));
    }

    let _ = child.kill();
    let _ = child.wait();
    Err("ローカルAPIが5秒以内に応答しませんでした".to_string())
}

/// 保存先をOSのダイアログで選ばせ、選ばれた場所を返す（取り消しなら None）。
///
/// ブラウザの保存先選択（`showSaveFilePicker`）は「このサイトにファイルの編集を
/// 許可するか」という警告が必ず付き、その文言は変えられない。ローカルの保存なのに
/// 身に覚えのない警告が出るため、OSのダイアログを使う。
///
/// ここでは場所を決めるだけで、書き込みは行わない。実際の複製は、元のファイルを
/// 持っているローカルAPIが受け持つ（大きな動画をこの層へ通さずに済む）。
#[tauri::command]
async fn choose_save_path(app: tauri::AppHandle, suggested_name: String) -> Option<String> {
    app.dialog()
        .file()
        .set_file_name(suggested_name)
        .blocking_save_file()
        .and_then(|chosen| chosen.into_path().ok())
        .map(|path| path.to_string_lossy().into_owned())
}

/// アンインストーラが「アプリデータも削除」を選んだときに渡す指定。
///
/// インストーラが標準で消すのは識別子の名前のフォルダだけで、作品の保存先は知らない。
/// 保存先は `paths.json`（このインストールの設定）で変えられるため、どこを消すかは
/// 画面を開くときと同じ決め方（`current_locations`）でここが決める。
const REMOVE_USER_DATA_FLAG: &str = "--remove-user-data";

/// 作品の保存先から、このアプリが作ったものを消し、置き場所の設定（`paths.json`）も消す。
/// 戻り値は終了コード。
///
/// ComfyUI本体とモデルの共有フォルダは消さない（別のインストールや開発環境と共用する
/// ことがあり、数十GBを取り直すことになるため）。
fn remove_user_data() -> i32 {
    // 置き場所の設定が読めないなら、どこを消すべきか分からない。既定の場所を
    // 消してしまうより、何もしないほうがよい。
    if let Err(error) = stored_locations() {
        eprintln!("{error}");
        return 1;
    }
    let data_dir = current_locations().data_dir;
    if data_dir.trim().is_empty() {
        return 1;
    }
    // インストール先では、同梱物は実行ファイルの隣に置かれる。
    let beside_exe = std::env::current_exe()
        .ok()
        .and_then(|exe| exe.parent().map(PathBuf::from));
    let Some(runtime) = development_runtime().or_else(|| beside_exe.and_then(bundled_runtime_at))
    else {
        eprintln!("アプリ用Python環境が見つかりません");
        return 1;
    };
    let status = app_module_command(
        &runtime,
        "chotto_sashie_shared.data_removal",
        &["--data-dir".to_string(), data_dir],
    )
    .status();
    // 置き場所の設定もこのインストールのデータ。アンインストーラが消すのは自分が入れた
    // ファイルだけで、アプリが後から作ったこの設定は残る。残すと、入れ直したときに
    // 消したはずの保存先が初期値として出てくる。
    if let Some(path) = locations_file() {
        if let Err(error) = std::fs::remove_file(&path) {
            if error.kind() != std::io::ErrorKind::NotFound {
                eprintln!("置き場所の設定を消せません（{}）: {error}", path.display());
            }
        }
    }
    match status {
        Ok(status) if status.success() => 0,
        Ok(_) => 1,
        Err(error) => {
            eprintln!("保存先を片付けられません: {error}");
            1
        }
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    if std::env::args().any(|argument| argument == REMOVE_USER_DATA_FLAG) {
        std::process::exit(remove_user_data());
    }
    // このアプリがAPIを起動するときだけ使う保護トークン。既存API再利用時は使わない。
    let token = Uuid::new_v4().to_string();
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![
            choose_save_path,
            choose_directory,
            get_locations,
            plan_data_dir_change,
            apply_data_dir_change,
            set_shared_dir
        ])
        .setup(move |app| {
            // 起動できなかった理由は画面に出す。黙って別のAPIへ繋がるより、
            // 何が起きているか分かるほうがよい。
            let mut startup_error: Option<String> = None;
            let (port, api_process) = start_local_api(app.handle(), &token).unwrap_or_else(|error| {
                eprintln!("{error}");
                startup_error = Some(error);
                (PREFERRED_API_PORT, None)
            });
            // このアプリがAPIを起動した場合だけ、トークンを保持してWebViewへ注入する。
            let owned_token = if api_process.is_some() {
                Some(token.clone())
            } else {
                None
            };
            app.manage(ApiProcess {
                child: Mutex::new(api_process),
                token: owned_token.clone(),
                port,
            });

            let mut builder = WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                .title("ちょっと挿絵")
                .inner_size(1280.0, 820.0)
                .min_inner_size(720.0, 640.0)
                .resizable(true)
                .visible(true)
                .focused(true);
            // 使う番号は起動のたびに変わりうるので、画面側へ教える。
            // ここで渡さないと、画面は決め打ちの番号へ繋ぎに行って空振りする。
            builder = builder.initialization_script(&format!(
                "window.__CHOTTO_SASHIE_API__ = \"http://127.0.0.1:{port}\";"
            ));
            if let Some(value) = &owned_token {
                // ページの読み込み前にトークンを渡す。フロントはこれを全fetchへ付与する。
                builder = builder.initialization_script(&format!(
                    "window.__CHOTTO_SASHIE_TOKEN__ = {value:?};"
                ));
            }
            if let Some(message) = &startup_error {
                builder = builder.initialization_script(&format!(
                    "window.__CHOTTO_SASHIE_STARTUP_ERROR__ = {message:?};"
                ));
            }
            builder.build()?;
            Ok(())
        })
        .on_window_event(|window, event| {
            if matches!(event, WindowEvent::Destroyed) {
                let process = window.state::<ApiProcess>();
                let mut child = process.child.lock().expect("APIプロセスの状態を取得できません");
                if let Some(child_process) = child.as_mut() {
                    stop_managed_runtime_before_api_exit(process.port, process.token.as_deref());
                    let _ = child_process.kill();
                    let _ = child_process.wait();
                }
                *child = None;
            }
        })
        .run(tauri::generate_context!())
        .expect("デスクトップアプリの起動に失敗しました");
}

/// 途中経過を画面へ届けるときの名前。画面はこの名前で受け取る。
const DATA_DIR_PROGRESS_EVENT: &str = "data-dir-progress";

/// 同梱のPythonで、アプリのモジュールを1回だけ動かす。
///
/// 保存先を移す処理は、APIを止めた状態で行う必要がある（自分のDBを掴んだままでは
/// 動かせない）。そのためAPIとは別に、単発の子プロセスとして呼ぶ。
///
/// 出力は1行1件のJSON。`progress` を持つ行は途中経過として `progress_event` の名前で
/// 画面へ流し、それ以外の**最後の1行**を結果とする。終わるまで待ってからまとめて
/// 読むと、数十GBの複製の間、画面に出せるものが何も無くなる。
/// アプリのPythonで、共通モジュールを単発で動かすコマンドを組み立てる。
fn app_module_command(runtime: &ApiRuntime, module: &str, arguments: &[String]) -> Command {
    let mut command = Command::new(&runtime.python);
    command.current_dir(&runtime.working_directory);
    if runtime.resources.is_none() {
        // 開発時は、リポジトリの共通モジュールを読ませる。
        if let Ok(repository) = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../..")
            .canonicalize()
        {
            command.env("PYTHONPATH", repository.join("services/shared/src"));
        }
    }
    command.arg("-m").arg(module).args(arguments);
    // 出力はUTF-8で受け取る。日本語のWindowsでは既定がcp932になり、理由の文が
    // そのままでは読めない字に化ける。
    command.env("PYTHONIOENCODING", "utf-8");

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    command
}

fn run_app_module(
    app: &tauri::AppHandle,
    module: &str,
    arguments: &[String],
    progress_event: Option<&str>,
) -> Result<serde_json::Value, String> {
    let runtime =
        api_runtime(app).ok_or_else(|| "アプリ用Python環境が見つかりません".to_string())?;
    let mut command = app_module_command(&runtime, module, arguments);

    command.stdout(Stdio::piped()).stderr(Stdio::piped());
    let mut child = command
        .spawn()
        .map_err(|error| format!("処理を始められません: {error}"))?;

    // 標準エラーは別の糸で吸っておく。こちらが標準出力を読み終えるまで放っておくと、
    // 出力が溜まったところで子が書けなくなり、互いに待ち合ったまま止まる。
    let stderr = child.stderr.take();
    let stderr_reader = std::thread::spawn(move || {
        let mut collected = String::new();
        if let Some(mut handle) = stderr {
            let _ = handle.read_to_string(&mut collected);
        }
        collected
    });

    let mut last: Option<serde_json::Value> = None;
    if let Some(stdout) = child.stdout.take() {
        // 1行ずつ、読めない字があっても捨てずに読む（そこで打ち切ると、最後の
        // 結果の行まで届かなくなる）。
        for line in BufReader::new(stdout).split(b'\n') {
            let Ok(bytes) = line else { break };
            let line = String::from_utf8_lossy(&bytes);
            let trimmed = line.trim();
            if trimmed.is_empty() {
                continue;
            }
            let Ok(parsed) = serde_json::from_str::<serde_json::Value>(trimmed) else {
                continue;
            };
            match (parsed.get("progress"), progress_event) {
                (Some(progress), Some(event)) => {
                    let _ = app.emit(event, progress.clone());
                }
                (Some(_), None) => {}
                (None, _) => last = Some(parsed),
            }
        }
    }
    let _ = child.wait();
    let errors = stderr_reader.join().unwrap_or_default();

    let parsed = last.ok_or_else(|| format!("応答を読めません: {errors}"))?;
    if let Some(message) = parsed.get("error").and_then(|value| value.as_str()) {
        return Err(message.to_string());
    }
    Ok(parsed)
}

/// いま使っている保存先・共有フォルダと、その決まり方を返す。
#[tauri::command]
fn get_locations() -> Locations {
    current_locations()
}

/// フォルダをOSのダイアログで選ばせる（取り消しなら None）。
#[tauri::command]
async fn choose_directory(app: tauri::AppHandle, title: String) -> Option<String> {
    app.dialog()
        .file()
        .set_title(&title)
        .blocking_pick_folder()
        .and_then(|chosen| chosen.into_path().ok())
        .map(|path| path.to_string_lossy().into_owned())
}

/// 移し先を見比べて、どの選び方ができるかを返す（何も変えない）。
#[tauri::command]
async fn plan_data_dir_change(
    app: tauri::AppHandle,
    target: String,
) -> Result<serde_json::Value, String> {
    let source = current_locations().data_dir;
    tauri::async_runtime::spawn_blocking(move || {
        run_app_module(
            &app,
            "chotto_sashie_shared.data_location",
            &[
                "--from".into(),
                source,
                "--to".into(),
                target,
                "--mode".into(),
                "migrate".into(),
                "--plan-only".into(),
            ],
            None,
        )
    })
    .await
    .map_err(|error| format!("下見に失敗しました: {error}"))?
}

/// 保存先を変える。終わったらアプリを再起動する（APIも画面も新しい場所で開き直す）。
///
/// 再起動で済ませるのは、途中の状態を持ち回らないため。APIだけ入れ替えると、
/// 画面が古い保存先の内容を掴んだまま新しいAPIと話すことになる。
#[tauri::command]
async fn apply_data_dir_change(
    app: tauri::AppHandle,
    target: String,
    mode: String,
) -> Result<(), String> {
    let source = current_locations().data_dir;
    let shared = current_locations().shared_dir;
    stop_owned_api(&app);

    let handle = app.clone();
    let moved_target = target.clone();
    let moved_mode = mode.clone();
    tauri::async_runtime::spawn_blocking(move || {
        run_app_module(
            &handle,
            "chotto_sashie_shared.data_location",
            &[
                "--from".into(),
                source,
                "--to".into(),
                moved_target,
                "--mode".into(),
                moved_mode,
            ],
            Some(DATA_DIR_PROGRESS_EVENT),
        )
    })
    .await
    .map_err(|error| format!("保存先を変えられませんでした: {error}"))??;

    write_locations(&target, &shared)?;
    app.restart();
}

/// 共有フォルダ（ComfyUI本体とモデルの置き場）を変える。中身は移さない。
///
/// 数十GBあるものを黙って動かさない。新しい場所にすでに一式があればそれを使い、
/// 無ければ取得し直すことになる——その判断は利用者に委ねる。
#[tauri::command]
async fn set_shared_dir(app: tauri::AppHandle, target: String) -> Result<(), String> {
    let data_dir = current_locations().data_dir;
    stop_owned_api(&app);
    write_locations(&data_dir, &target)?;
    app.restart();
}

/// このアプリが起動したAPIだけを止める。相乗り中なら何もしない。
fn stop_owned_api(app: &tauri::AppHandle) {
    let process = app.state::<ApiProcess>();
    let mut child = process.child.lock().expect("APIプロセスの状態を取得できません");
    if let Some(running) = child.as_mut() {
        stop_managed_runtime_before_api_exit(process.port, process.token.as_deref());
        let _ = running.kill();
        let _ = running.wait();
    }
    *child = None;
}

#[cfg(test)]
mod tests {
    use super::normalize_path;

    #[test]
    fn compares_data_dirs_ignoring_separator_style() {
        // 環境変数にはスラッシュで書けるが、APIは円記号で答える。揃えずに比べると
        // 同じ場所を「別の保存先」と誤判定し、相乗りできるAPIを避けて二重に起動する。
        assert_eq!(normalize_path("D:/t/data"), normalize_path(r"D:\t\data"));
        // 末尾の区切りと大文字小文字も、見た目の違いにすぎない。
        assert_eq!(normalize_path(r"D:\T\Data\\"), normalize_path("d:/t/data"));
        // 別の場所は別のままにする。
        assert_ne!(normalize_path("D:/t/data"), normalize_path("D:/t/other"));
    }
}
