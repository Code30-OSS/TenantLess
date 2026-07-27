/**
 * ThemeToggle — flips the AppShell wrapper's `[data-theme]` between `dark` (default) and `light`.
 *
 * Controlled: AppShell owns the theme state and the toggle handler (passed down through Topbar), so
 * the single source of truth for the active palette is the `[data-theme]` attribute on the shell
 * wrapper. Styles are borrowed from Topbar.module.css (ThemeToggle has no CSS module of its own).
 */
import type { Theme } from './AppShell';
import styles from './Topbar.module.css';

interface ThemeToggleProps {
  theme: Theme;
  onToggle: () => void;
}

export default function ThemeToggle({ theme, onToggle }: ThemeToggleProps) {
  const isDark = theme === 'dark';
  return (
    <button
      type="button"
      className={styles.themeToggle}
      onClick={onToggle}
      aria-label={isDark ? 'Switch to light theme' : 'Switch to dark theme'}
    >
      <span className={isDark ? styles.themeSegActive : styles.themeSeg}>☾ dark</span>
      <span className={!isDark ? styles.themeSegActive : styles.themeSeg}>☀ light</span>
    </button>
  );
}
