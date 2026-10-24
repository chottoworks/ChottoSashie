import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 1421,
    strictPort: true,
    proxy: {
      // 既定は開発用APIの8766番。配布版を入れて動かしていると8766番はそちらが使うので、
      // 開発用APIを別の番号で動かすときは CHOTTO_SASHIE_API_URL で中継先を変える
      // （変えずに開くと、配布版のAPI＝実データへつながる）。
      "/api": process.env.CHOTTO_SASHIE_API_URL ?? "http://127.0.0.1:8766",
    },
  },
  clearScreen: false,
});
