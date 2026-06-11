import { defineConfig } from 'wxt';

// See https://wxt.dev/api/config.html
export default defineConfig({
  extensionApi: 'webextension-polyfill',
  manifestVersion: 3,
  srcDir: 'src',
  outDir: 'dist',
  runner: {
    disabled: true, // no browser binary in CI
  },
  manifest: {
    permissions: [
      'storage',
      'tabs',
      'webNavigation',
      'scripting',
    ],
    host_permissions: ['<all_urls>'],
  },
});
