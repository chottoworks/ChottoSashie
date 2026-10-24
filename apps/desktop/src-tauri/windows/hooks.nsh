; インストーラの追加処理（tauri.conf.json の bundle.windows.nsis.installerHooks から読む）。

; アンインストールで「アプリデータも削除」を選んだときに、作品の保存先も片付ける。
;
; 標準で消えるのは識別子の名前のフォルダ（%LOCALAPPDATA%\jp.chottoworks.sashie など）
; だけで、作品の保存先（既定は %LOCALAPPDATA%\ChottoSashie、paths.json で変更可）は
; 残ってしまう。どこが保存先かはアプリ自身が決めるので、アプリに消させる。
; 実行ファイルと同梱のPythonを使うため、ファイルを消す前（PREUNINSTALL）に呼ぶ。
; ComfyUI本体とモデルの共有フォルダは消さない（別のインストールと共用することがある）。
!macro NSIS_HOOK_PREUNINSTALL
  ${If} $DeleteAppDataCheckboxState = 1
  ${AndIf} $UpdateMode <> 1
    ; この追加処理は、標準の「アプリが動いていないか」の確認より先に呼ばれる。動いている
    ; アプリ（とその子のAPI・ComfyUI）が保存先のファイルを掴んだままだと消せないので、
    ; 先に同じ確認をして閉じてもらう（標準の確認は、そのあと何もせずに通る）。
    !insertmacro CheckIfAppIsRunning "${MAINBINARYNAME}.exe" "${PRODUCTNAME}"
    ExecWait '"$INSTDIR\${MAINBINARYNAME}.exe" --remove-user-data'
  ${EndIf}
!macroend
