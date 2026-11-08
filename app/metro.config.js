const { getDefaultConfig } = require("expo/metro-config");
const { createDevApiProxy } = require("./devApiProxy.cjs");

const config = getDefaultConfig(__dirname);
// Expo CLI loads .env.local before this file. The proxy is opt-in, server-only,
// and used only by the running development server, not exported/native bundles.
const proxy = createDevApiProxy({target: process.env.AMBIENT_DEV_API_PROXY_TARGET});
if (proxy) {
  const enhance = config.server.enhanceMiddleware;
  config.server.enhanceMiddleware = (middleware, server) => {
    const next = enhance ? enhance(middleware, server) : middleware;
    return (req, res, done) => proxy(req, res, () => next(req, res, done));
  };
}

module.exports = config;
