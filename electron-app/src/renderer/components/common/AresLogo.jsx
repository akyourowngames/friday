export function AresLogo({ size = 24, className = "" }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 64 64"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className={className}
      aria-label="Ares"
    >
      {/* Helmet crest */}
      <path
        d="M32 4 L36 14 L38 8 L40 14 L38 18 L32 16 L26 18 L24 14 L26 8 L28 14 Z"
        fill="currentColor"
        opacity="0.9"
      />

      {/* Helmet dome */}
      <path
        d="M16 28 C16 14 23 8 32 8 C41 8 48 14 48 28 L48 32 L44 34 L44 28 C44 20 39 14 32 14 C25 14 20 20 20 28 L20 34 L16 32 Z"
        fill="currentColor"
      />

      {/* Eye slit */}
      <path
        d="M22 30 L28 28 L36 28 L42 30 L40 32 L36 31 L28 31 L24 32 Z"
        fill="rgba(14,14,18,0.95)"
      />

      {/* Nose guard */}
      <path
        d="M30 30 L32 38 L34 30 Z"
        fill="currentColor"
        opacity="0.7"
      />

      {/* Cheek guard left */}
      <path
        d="M16 32 L18 28 L20 34 L18 44 L14 48 L14 36 Z"
        fill="currentColor"
        opacity="0.85"
      />

      {/* Cheek guard right */}
      <path
        d="M48 32 L46 28 L44 34 L46 44 L50 48 L50 36 Z"
        fill="currentColor"
        opacity="0.85"
      />

      {/* Chin guard */}
      <path
        d="M18 44 L22 40 L28 42 L32 46 L36 42 L42 40 L46 44 L44 52 L32 56 L20 52 Z"
        fill="currentColor"
        opacity="0.75"
      />

      {/* Highlight line */}
      <path
        d="M24 16 C26 13 29 11 32 11 C35 11 38 13 40 16"
        stroke="currentColor"
        strokeWidth="1"
        strokeLinecap="round"
        fill="none"
        opacity="0.3"
      />
    </svg>
  );
}
