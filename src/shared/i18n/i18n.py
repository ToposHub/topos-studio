"""
Internationalization (i18n) module for Topos Color.
Provides translation management with JSON-based language files.
"""
import json
import os
import logging

logger = logging.getLogger(__name__)
from typing import Dict, Optional, Any


class I18N:
    """
    Internationalization manager for handling multi-language support.
    
    Usage:
        I18N.load_locale("zh_CN")
        text = I18N.tr("menu.file")  # Returns "文件(&F)"
    """
    
    _instance: Optional['I18N'] = None
    _translations: Dict[str, Any] = {}
    _fallback_translations: Dict[str, Any] = {}
    _current_locale: str = "en_US"
    _fallback_locale: str = "en_US"

    # Supported locales with display names
    # Locale codes follow the ISO standard format: {language}_{REGION}
    # - en_US: English (United States) - use for all English variants
    # - zh_CN: 简体中文 (Simplified Chinese)
    # To add more locales: create {locale_code}.json in every locales/ dir
    # (src/shared/i18n/locales/ and src/features/*/locales/) and add an entry here.
    SUPPORTED_LOCALES: Dict[str, str] = {
        "en_US": "English",
        "zh_CN": "简体中文",
        "ja_JP": "日本語",
        "ko_KR": "한국어",
        "fr_FR": "Français",
        "de_DE": "Deutsch",
        "es_ES": "Español",
        "pt_BR": "Português (Brasil)",
        "ru_RU": "Русский",
        "it_IT": "Italiano",
    }
    
    # Locales directory path (set during initialization)
    _locales_dir: Optional[str] = None

    # Tracked module locales dirs for reload support
    _module_locales_dirs: list[str] = []
    
    @classmethod
    def initialize(cls, locales_dir: Optional[str] = None) -> None:
        """
        Initialize the i18n system with the locales directory path.
        
        Args:
            locales_dir: Path to the locales directory. If None, uses default path.
        """
        if locales_dir is None:
            # Default: src/shared/i18n/locales/ relative to this file
            current_dir = os.path.dirname(os.path.abspath(__file__))
            cls._locales_dir = os.path.join(current_dir, "locales")
        else:
            cls._locales_dir = locales_dir
    
    @classmethod
    def get_locales_dir(cls) -> str:
        """Get the locales directory path."""
        if cls._locales_dir is None:
            cls.initialize()
        return cls._locales_dir
    
    @classmethod
    def load_locale(cls, locale_code: str) -> bool:
        """
        Load a locale's translation file.

        Args:
            locale_code: The locale code (e.g., "en_US", "zh_CN")

        Returns:
            True if loading succeeded, False otherwise
        """
        if cls._locales_dir is None:
            cls.initialize()

        if locale_code not in cls.SUPPORTED_LOCALES:
            logger.warning(f"Warning: Unsupported locale '{locale_code}', falling back to '{cls._fallback_locale}'")
            locale_code = cls._fallback_locale

        file_path = os.path.join(cls._locales_dir, f"{locale_code}.json")

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                cls._translations = json.load(f)
            if not isinstance(cls._translations, dict):
                # AUD-33801：json.load 接受任意合法 JSON 根；非 dict 根
                # （手工编辑/截断产生的 list/str/number）会让后续模块合并
                # 路径的 _deep_merge 抛未捕获异常，所有 feature 模块激活
                # 依赖该合并——按坏文件处理。
                logger.error(
                    f"Error: Translation file {file_path} root is "
                    f"{type(cls._translations).__name__}, expected a JSON object"
                )
                cls._translations = {}
                return False
            cls._current_locale = locale_code

            # Always load fallback (English) translations for missing keys
            cls._load_fallback_translations()

            return True
        except FileNotFoundError:
            logger.error(f"Error: Translation file not found: {file_path}")
            # Try to load fallback locale
            if locale_code != cls._fallback_locale:
                return cls.load_locale(cls._fallback_locale)
            return False
        except json.JSONDecodeError as e:
            logger.error(f"Error: Invalid JSON in translation file: {e}")
            return False
    
    @classmethod
    def _load_fallback_translations(cls) -> None:
        """
        Load fallback (English) translations for missing keys.
        Called automatically when loading any locale.
        """
        fallback_path = os.path.join(cls._locales_dir, f"{cls._fallback_locale}.json")
        try:
            with open(fallback_path, "r", encoding="utf-8") as f:
                cls._fallback_translations = json.load(f)
            if not isinstance(cls._fallback_translations, dict):
                # AUD-33801：与主词典同规则——非 dict 根按坏文件降级为 {}。
                logger.warning(
                    f"Warning: Fallback translation file {fallback_path} root is "
                    f"{type(cls._fallback_translations).__name__}, expected a JSON object"
                )
                cls._fallback_translations = {}
        except FileNotFoundError:
            logger.warning(f"Warning: Fallback translation file not found: {fallback_path}")
            cls._fallback_translations = {}
        except json.JSONDecodeError as e:
            logger.warning(f"Warning: Invalid JSON in fallback file: {e}")
            cls._fallback_translations = {}
    
    @classmethod
    def tr(cls, key: str, default: Optional[str] = None, **kwargs) -> str:
        """
        Get translated text for a given key.

        Args:
            key: Dot-separated key path (e.g., "menu.file", "panel.layers")
            default: Default text if key not found. If None, returns English fallback.
            **kwargs: Format arguments for string interpolation

        Returns:
            Translated text, or English fallback/default if not found
        """
        if not cls._translations:
            if default is None:
                return key
            # The untranslated default may still carry placeholders (e.g.
            # "unavailable: {error}"); interpolate them or the user sees the
            # literal braces.
            if kwargs:
                try:
                    return str(default).format(**kwargs)
                except (KeyError, IndexError, ValueError):
                    return str(default)
            return default

        # Navigate through nested dictionary using dot notation
        keys = key.split(".")
        value = cls._translations

        try:
            for k in keys:
                value = value[k]

            # Handle string interpolation if kwargs provided
            if isinstance(value, str) and kwargs:
                try:
                    return value.format(**kwargs)
                except (KeyError, IndexError, ValueError) as e:
                    # AUD-33800：译文是外部数据——孤立 '{'/'}' 抛
                    # ValueError、位置占位符抛 IndexError，必须与缺失
                    # 参数（KeyError）同样降级为原文，否则异常直达 UI
                    # 调用点（与下方 default 分支的捕获集对齐）。
                    logger.warning(
                        f"Warning: Format error {e!r} for key '{key}' "
                        f"(template {value!r}, kwargs {list(kwargs)})"
                    )
                    return value

            return value if isinstance(value, str) else str(value)
        except (KeyError, TypeError):
            # Translation key not found in current locale
            # Try fallback (English) translations
            if cls._fallback_translations:
                try:
                    fallback_value = cls._fallback_translations
                    for k in keys:
                        fallback_value = fallback_value[k]
                    
                    # Handle string interpolation for fallback value
                    if isinstance(fallback_value, str) and kwargs:
                        try:
                            return fallback_value.format(**kwargs)
                        except (KeyError, IndexError, ValueError) as e:
                            # AUD-33800：与主路径同规则（见上）。
                            logger.warning(
                                f"Warning: Format error {e!r} for key '{key}' "
                                f"in fallback (template {fallback_value!r}, "
                                f"kwargs {list(kwargs)})"
                            )
                            return fallback_value
                    
                    return fallback_value if isinstance(fallback_value, str) else str(fallback_value)
                except (KeyError, TypeError):
                    # Key not found in fallback either
                    pass
            
            # Neither current locale nor fallback has the key
            if default is not None:
                # 与上方空词典早退分支同规则：default 可能携带占位符
                # （如 "{name}: ..."），不插值会把字面花括号送到 UI。
                if kwargs:
                    try:
                        return str(default).format(**kwargs)
                    except (KeyError, IndexError, ValueError):
                        return str(default)
                return default
            # Return the key as last resort
            return key

    @classmethod
    def tr_or_none(cls, key: str, **kwargs) -> Optional[str]:
        """Translate ``key``; return ``None`` when no translation exists.

        ``I18N.tr(key, default=None)`` returns the *literal key string* when
        the key is missing (a UI-friendly fallback for toast messages, etc.),
        which makes ``if translated:`` checks in callers incorrectly treat a
        missing translation as found. This helper returns ``None`` for missing
        keys so callers can fall back to their own sensible default.

        Args:
            key: Dot-separated key path (e.g., "nodes.crop.title").
            **kwargs: Optional format arguments for string interpolation.

        Returns:
            The translated string, or ``None`` if no translation exists for
            ``key`` in either the current locale or the fallback.
        """
        if not cls._translations:
            return None
        result = cls.tr(key, default=None, **kwargs)
        # tr() returns the literal key when nothing was found.
        if result == key:
            return None
        return result

    @classmethod
    def get_current_locale(cls) -> str:
        """Get the currently loaded locale code."""
        return cls._current_locale
    
    @classmethod
    def get_available_locales(cls) -> Dict[str, str]:
        """Get dictionary of supported locale codes and their display names."""
        return cls.SUPPORTED_LOCALES.copy()
    
    @classmethod
    def get_locale_display_name(cls, locale_code: str) -> str:
        """Get the display name for a locale code."""
        return cls.SUPPORTED_LOCALES.get(locale_code, locale_code)
    
    @classmethod
    def is_locale_supported(cls, locale_code: str) -> bool:
        """Check if a locale code is supported."""
        return locale_code in cls.SUPPORTED_LOCALES
    
    @classmethod
    def load_module_translations(cls, module_locales_dir: str) -> None:
        """Load additional translations from a module's locales directory.

        Merges module translations into the existing translation dicts.
        Module keys override shared keys on conflict (module takes precedence).
        The dir is tracked so reload_locale() can re-merge after locale switch.

        Args:
            module_locales_dir: Absolute path to the module's locales directory.
        """
        if not os.path.isdir(module_locales_dir):
            return

        # Track for reload support
        if module_locales_dir not in cls._module_locales_dirs:
            cls._module_locales_dirs.append(module_locales_dir)

        cls._do_load_module_dir(module_locales_dir)

    @classmethod
    def _merged_module_dir(
        cls,
        translations: Dict[str, Any],
        fallback_translations: Dict[str, Any],
        module_locales_dir: str,
        locale_code: str,
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        """Merge one module dir into *copies* of the given dicts (AUD-33802).

        Pure helper: never touches class state, so :meth:`reload_locale` can
        build a complete candidate dictionary off to the side and swap it in
        atomically. Bad files are skipped with a warning, matching the
        historical behavior of ``_do_load_module_dir``.

        Returns:
            ``(merged_translations, merged_fallback_translations)``
        """
        translations = dict(translations)
        fallback_translations = dict(fallback_translations)
        loaded_current_locale = False

        # Load module's current locale translations
        locale_file = os.path.join(module_locales_dir, f"{locale_code}.json")
        if os.path.exists(locale_file):
            try:
                with open(locale_file, "r", encoding="utf-8") as f:
                    module_translations = json.load(f)
                if not isinstance(module_translations, dict):
                    # AUD-33801：非 dict 根无法参与 _deep_merge；按坏文件
                    # 跳过（记日志），不得让异常逃出模块激活路径。
                    raise ValueError(
                        f"root is {type(module_translations).__name__}, "
                        "expected a JSON object"
                    )
                translations = cls._deep_merge(translations, module_translations)
                loaded_current_locale = True
            except (json.JSONDecodeError, OSError, ValueError, TypeError, AttributeError) as e:
                # TypeError/AttributeError 为畸形根的兜底（AUD-33801）。
                logger.warning(f"Warning: Failed to load module translations from {locale_file}: {e}")

        # Load module's fallback translations
        fallback_file = os.path.join(module_locales_dir, f"{cls._fallback_locale}.json")
        if os.path.exists(fallback_file):
            try:
                with open(fallback_file, "r", encoding="utf-8") as f:
                    module_fallback = json.load(f)
                if not isinstance(module_fallback, dict):
                    raise ValueError(
                        f"root is {type(module_fallback).__name__}, "
                        "expected a JSON object"
                    )
                fallback_translations = cls._deep_merge(fallback_translations, module_fallback)
                if not loaded_current_locale:
                    translations = cls._deep_merge(translations, module_fallback)
            except (json.JSONDecodeError, OSError, ValueError, TypeError, AttributeError) as e:
                logger.warning(f"Warning: Failed to load module fallback from {fallback_file}: {e}")

        return translations, fallback_translations

    @classmethod
    def _do_load_module_dir(cls, module_locales_dir: str) -> None:
        """Internal: load and merge translations from a single module dir."""
        cls._translations, cls._fallback_translations = cls._merged_module_dir(
            cls._translations, cls._fallback_translations,
            module_locales_dir, cls._current_locale,
        )

    @classmethod
    def _read_locale_file(cls, path: str) -> Optional[Dict[str, Any]]:
        """Read one locale JSON file; dict root or None (AUD-33802 helper).

        Logs and returns None on missing / invalid / non-dict-root files.
        """
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            logger.error(f"Error: Translation file not found: {path}")
            return None
        except json.JSONDecodeError as e:
            logger.error(f"Error: Invalid JSON in translation file: {e}")
            return None
        if not isinstance(data, dict):
            logger.error(
                f"Error: Translation file {path} root is "
                f"{type(data).__name__}, expected a JSON object"
            )
            return None
        return data

    @classmethod
    def reload_locale(cls, locale_code: str) -> bool:
        """Reload base locale and re-merge all tracked module translations.

        Call this when switching language at runtime. It:
        1. Reloads the base locale file
        2. Re-merges all previously registered module translations

        AUD-33802：类级共享词典的切换不再走"先换基础词典、再逐个重合并
        模块"的两步原地更新——那个中间态窗口内，并发 ``tr()`` 会读到缺
        模块键的词典。现在完整合并结果先在线程私有的局部变量上构建，
        最后按属性整体替换（CPython 属性赋值原子）：无锁的读取方要么
        看到旧完整态、要么新完整态，不存在中间态。失败路径也不再污染
        活动词典（旧实现经 ``load_locale`` 失败时可能留下仅基础键的
        半成品词典）。

        Args:
            locale_code: The new locale code (e.g., "zh_CN", "en_US")

        Returns:
            True if reload succeeded, False otherwise
        """
        if cls._locales_dir is None:
            cls.initialize()

        if locale_code not in cls.SUPPORTED_LOCALES:
            logger.warning(f"Warning: Unsupported locale '{locale_code}', falling back to '{cls._fallback_locale}'")
            locale_code = cls._fallback_locale

        # Step 1: build the base locale dictionaries (local copies only).
        base_file = os.path.join(cls._locales_dir, f"{locale_code}.json")
        if not os.path.exists(base_file):
            # 与 load_locale 同语义：主文件缺失时回退 fallback locale；
            # 保持旧 reload_locale 的返回值契约（递归结果透传）。
            if locale_code != cls._fallback_locale:
                return cls.reload_locale(cls._fallback_locale)
            return False
        translations = cls._read_locale_file(base_file)
        if translations is None:
            # 文件存在但损坏（非法 JSON / 非 dict 根）：失败返回，不切换、
            # 不污染活动词典（AUD-33802：旧实现经 load_locale 也返回 False）。
            return False

        fallback_file = os.path.join(cls._locales_dir, f"{cls._fallback_locale}.json")
        fallback_translations = cls._read_locale_file(fallback_file)
        if fallback_translations is None:
            # _load_fallback_translations 的历史语义：fallback 读不到按
            # 空词典继续，不算整体失败。
            fallback_translations = {}

        # Step 2: re-merge all tracked module translations (still local).
        for module_dir in cls._module_locales_dirs:
            translations, fallback_translations = cls._merged_module_dir(
                translations, fallback_translations, module_dir, locale_code)

        # Step 3: atomic swap — readers never observe a partial state.
        cls._translations = translations
        cls._fallback_translations = fallback_translations
        cls._current_locale = locale_code

        logger.info(f"Locale reloaded to {locale_code} with {len(cls._module_locales_dirs)} module translations")
        return True

    @classmethod
    def _deep_merge(cls, base: dict, override: dict) -> dict:
        """Deep merge override into base dict. Returns merged dict."""
        result = dict(base)
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = cls._deep_merge(result[key], value)
            else:
                result[key] = value
        return result

    @classmethod
    def _locale_for_language(cls, language_code: str) -> Optional[str]:
        """Map a bare language code (e.g. "zh", "fr") to a supported locale.

        Matching is prefix-based against SUPPORTED_LOCALES, so "zh" resolves
        to zh_CN (the only supported Chinese variant) and "fr" to fr_FR.
        Returns None when no supported locale shares the language.
        """
        if not language_code:
            return None
        lang = language_code.lower()
        for locale_code in cls.SUPPORTED_LOCALES:
            if lang == locale_code.split("_")[0]:
                return locale_code
        return None

    @classmethod
    def get_system_locale(cls) -> str:
        """
        Detect the system language and return the best matching supported locale.

        Returns:
            The best matching locale code from SUPPORTED_LOCALES
        """
        import locale

        # Try multiple methods to get system language

        # Method 1: Get system default locale (most reliable on macOS/Linux)
        try:
            # Get the user's default locale as a string
            system_locale = locale.getdefaultlocale()[0]
            if system_locale:
                # Normalize to our format (e.g., "zh_CN", "en_US")
                # locale.getdefaultlocale() returns like "zh_CN" or "zh"
                matched = cls._locale_for_language(system_locale.split("_")[0])
                if matched:
                    return matched
        except Exception as e:
            logger.error(f"Warning: locale.getdefaultlocale() failed: {e}")

        # Method 2: Try environment variables
        env_locale = os.environ.get("LANG", "") or os.environ.get("LC_ALL", "")
        if env_locale:
            # LANG looks like "zh_CN.UTF-8" — take the language part
            matched = cls._locale_for_language(
                env_locale.replace(".", "_").split("_")[0])
            if matched:
                return matched

        # Method 3: Try Qt's system locale
        try:
            from PyQt6.QtCore import QLocale
            qt_locale = QLocale.system()
            # Get the language part
            language = qt_locale.languageToCode(qt_locale.language())
            matched = cls._locale_for_language(language)
            if matched:
                return matched
        except Exception as e:
            logger.error(f"Warning: QLocale detection failed: {e}")

        # Default fallback
        return "en_US"


# Convenience function for shorter syntax
def tr(key: str, default: Optional[str] = None, **kwargs) -> str:
    """
    Shorthand for I18N.tr()
    
    Args:
        key: Translation key
        default: Default text if key not found
        **kwargs: Format arguments
        
    Returns:
        Translated text
    """
    return I18N.tr(key, default, **kwargs)


def tr_or_none(key: str, **kwargs) -> Optional[str]:
    """Shorthand for I18N.tr_or_none().

    Translate ``key`` and return ``None`` when no translation exists (instead
    of returning the literal key string). Use this whenever the caller wants
    to fall back to its own default for missing translations.
    """
    return I18N.tr_or_none(key, **kwargs)
