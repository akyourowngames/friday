/** @type {import('next').NextConfig} */
const nextConfig = {
	output: "standalone",
	reactStrictMode: true,
	// node-pty is a native addon — keep it external to the server bundle.
	serverExternalPackages: ["node-pty"],
	// Allow the Next app to import directly from the parent friday-ng core
	// (../src/*). The aliases are also declared in tsconfig.json for typecheck.
	outputFileTracingRoot: process.cwd(),
	webpack(config) {
		// Resolve .ts and .tsx as if they were .js / .jsx, so cross-package
		// imports from ../src/ (which always use the .ts extension) work
		// without forcing the parent repo to compile first.
		config.resolve.extensionAlias = {
			...config.resolve.extensionAlias,
			".js": [".ts", ".tsx", ".js", ".jsx"],
			".mjs": [".mts", ".mjs"],
		};
		return config;
	},
};

export default nextConfig;

