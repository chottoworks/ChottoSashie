import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { PreferencesProvider } from "./preferences";
import "./styles.css";

// デスクトップ版は起動時に window.__CHOTTO_SASHIE_TOKEN__ を注入する。
// あればすべての fetch にトークンヘッダを付け、他プロセスからのAPI操作を防ぐ。
const apiToken = (window as unknown as { __CHOTTO_SASHIE_TOKEN__?: string }).__CHOTTO_SASHIE_TOKEN__;
if (apiToken) {
  const originalFetch = window.fetch.bind(window);
  window.fetch = (input, init) => {
    const headers = new Headers(init?.headers);
    headers.set("X-Chotto-Sashie-Token", apiToken);
    return originalFetch(input, { ...init, headers });
  };
}

// 画面の外から持ち込んだファイルは受け付けない。落とすと、そのファイルを開きに行って
// 画面が置き換わってしまう。受け付けない印のカーソルを出し、落としても何もしない。
window.addEventListener("dragover", (event) => {
  if (!event.dataTransfer?.types.includes("Files")) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = "none";
});
window.addEventListener("drop", (event) => {
  if (event.dataTransfer?.types.includes("Files")) event.preventDefault();
});

const root = document.getElementById("root");
if (!root) {
  throw new Error("アプリの表示先を初期化できませんでした");
}

createRoot(root).render(
  <StrictMode>
    {/* 前回選んだ生成方法・枚数は、どの画面からも同じものを読む。 */}
    <PreferencesProvider>
      <App />
    </PreferencesProvider>
  </StrictMode>,
);
