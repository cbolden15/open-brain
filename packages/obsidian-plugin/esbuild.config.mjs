import esbuild from "esbuild";
import { copyFile, mkdir } from "node:fs/promises";
import process from "node:process";

const production = process.argv[2] === "production";
const output = new URL("../../build/obsidian-plugin/open-brain/", import.meta.url);

await mkdir(output, { recursive: true });
await esbuild.build({
  entryPoints: ["src/main.ts"],
  bundle: true,
  external: ["obsidian", "electron"],
  format: "cjs",
  logLevel: "info",
  minify: production,
  outfile: new URL("main.js", output).pathname,
  platform: "node",
  sourcemap: production ? false : "inline",
  target: "es2021",
  treeShaking: true,
});

await Promise.all([
  copyFile(new URL("manifest.json", import.meta.url), new URL("manifest.json", output)),
  copyFile(new URL("styles.css", import.meta.url), new URL("styles.css", output)),
]);
