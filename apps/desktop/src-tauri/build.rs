fn main() {
    // アイコンだけの変更でも、Windowsの実行ファイルへ埋め込み直す。
    println!("cargo:rerun-if-changed=icons/icon.ico");
    println!("cargo:rerun-if-changed=icons/icon.png");
    tauri_build::build()
}
