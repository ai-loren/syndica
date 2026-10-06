import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// 产物随 Python 包发布：用户不需要 Node。文件名不带哈希，产物目录的哈希只随内容变。
export default defineConfig({
  plugins: [react()],
  base: '/',
  build: {
    outDir: '../src/syndica/server/static',
    emptyOutDir: true,
    sourcemap: false,
    rollupOptions: {
      output: {
        entryFileNames: 'assets/app.js',
        chunkFileNames: 'assets/[name].js',
        assetFileNames: 'assets/[name][extname]',
      },
    },
  },
});
