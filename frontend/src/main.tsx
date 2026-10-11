import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import AuthGate from "./AuthGate.tsx";
import "./styles.css";
import "./auth.css";

const root = document.getElementById("root");
if (!root) throw new Error("画面の起動先が見つかりません。");
createRoot(root).render(<StrictMode><AuthGate /></StrictMode>);
