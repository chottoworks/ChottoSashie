// リリース版でコンソールを開かせない。付けないとWindowsが黒い窓を割り当て、
// WebView2の内部ログまで利用者に見える。開発版では付けず、ログを読めるようにする。
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    chotto_sashie_desktop_lib::run();
}
