"""
Theme Definitions for Loop Station.
Includes button text contrast fixes and new Smart Cut (Skip) colors.
"""

DEFAULT_THEME = "Midnight Blue"

THEMES = {
    # =========================================================================
    # GROUP 1: PRO DARK
    # =========================================================================
    "Midnight Blue": {
        "bg_primary": "#111111", "bg_secondary": "#1a1a1a",
        "fg_primary": "#4fa3e0", "text_main": "#ffffff", "text_dim": "#888888",
        "waveform_fg": "#4fa3e0", "waveform_bg": "#111111", "accent_warn": "#d68f29",
        "btn_success": "#2cc985", "btn_danger": "#d63031", "btn_text": "#ffffff",
        # Smart Cut Colors
        "skip_region": "#882222", "skip_candidate": "#ff5555", "btn_skip": "#d63031"
    },
    "Stage Black": {
        "bg_primary": "#000000", "bg_secondary": "#080808",
        "fg_primary": "#777777", "text_main": "#cccccc", "text_dim": "#555555",
        "waveform_fg": "#777777", "waveform_bg": "#000000", "accent_warn": "#884444",
        "btn_success": "#336633", "btn_danger": "#663333", "btn_text": "#ffffff",
        # Smart Cut Colors
        "skip_region": "#662222", "skip_candidate": "#cc4444", "btn_skip": "#883333"
    },
    "Red Room": {
        "bg_primary": "#1a0505", "bg_secondary": "#2b0a0a",
        "fg_primary": "#cc3333", "text_main": "#ffcccc", "text_dim": "#884444",
        "waveform_fg": "#cc3333", "waveform_bg": "#1a0505", "accent_warn": "#ff6666",
        "btn_success": "#cc3333", "btn_danger": "#ff0000", "btn_text": "#ffffff",
        # Smart Cut Colors (Brighter to contrast with red bg)
        "skip_region": "#ff4444", "skip_candidate": "#ffffff", "btn_skip": "#ff0000"
    },
    "Deep Space": {
        "bg_primary": "#0f0f16", "bg_secondary": "#181824",
        "fg_primary": "#7d6bc4", "text_main": "#e0e0ff", "text_dim": "#666688",
        "waveform_fg": "#7d6bc4", "waveform_bg": "#0f0f16", "accent_warn": "#aa5566",
        "btn_success": "#55aa55", "btn_danger": "#aa3333", "btn_text": "#ffffff",
        # Smart Cut Colors
        "skip_region": "#882244", "skip_candidate": "#ff6688", "btn_skip": "#cc3355"
    },
    "Slate": {
        "bg_primary": "#1e1e1e", "bg_secondary": "#252525",
        "fg_primary": "#777777", "text_main": "#dddddd", "text_dim": "#666666",
        "waveform_fg": "#999999", "waveform_bg": "#1e1e1e", "accent_warn": "#aaaaaa",
        "btn_success": "#666666", "btn_danger": "#444444", "btn_text": "#ffffff",
        # Smart Cut Colors
        "skip_region": "#553333", "skip_candidate": "#995555", "btn_skip": "#664444"
    },

    # =========================================================================
    # GROUP 2: REHEARSAL LIGHT
    # =========================================================================
    "Paper White": {
        "bg_primary": "#ffffff", "bg_secondary": "#f2f2f2",
        "fg_primary": "#333333", "text_main": "#000000", "text_dim": "#666666",
        "waveform_fg": "#111111", "waveform_bg": "#ffffff", "accent_warn": "#cc0000",
        "btn_success": "#009900", "btn_danger": "#cc0000", "btn_text": "#ffffff",
        # Smart Cut Colors (Darker region to show on white)
        "skip_region": "#ffcccc", "skip_candidate": "#ff0000", "btn_skip": "#cc0000"
    },
    "Solarized Light": {
        "bg_primary": "#fdf6e3", "bg_secondary": "#eee8d5",
        "fg_primary": "#268bd2", "text_main": "#586e75", "text_dim": "#93a1a1",
        "waveform_fg": "#268bd2", "waveform_bg": "#fdf6e3", "accent_warn": "#cb4b16",
        "btn_success": "#859900", "btn_danger": "#dc322f", "btn_text": "#ffffff",
        # Smart Cut Colors
        "skip_region": "#eebbbb", "skip_candidate": "#dc322f", "btn_skip": "#dc322f"
    },
    "Morning Fog": {
        "bg_primary": "#e6e6e6", "bg_secondary": "#d9d9d9",
        "fg_primary": "#556677", "text_main": "#222222", "text_dim": "#666666",
        "waveform_fg": "#556677", "waveform_bg": "#e6e6e6", "accent_warn": "#aa4444",
        "btn_success": "#448844", "btn_danger": "#aa4444", "btn_text": "#ffffff",
        # Smart Cut Colors
        "skip_region": "#cc8888", "skip_candidate": "#cc4444", "btn_skip": "#aa4444"
    },
    "Blueprint": {
        "bg_primary": "#204060", "bg_secondary": "#1a3350",
        "fg_primary": "#ffffff", "text_main": "#ffffff", "text_dim": "#80a0c0",
        "waveform_fg": "#ffffff", "waveform_bg": "#204060", "accent_warn": "#ffaaaa",
        "btn_success": "#44cc44", "btn_danger": "#ff4444", "btn_text": "#204060",
        # Smart Cut Colors
        "skip_region": "#aa4444", "skip_candidate": "#ff8888", "btn_skip": "#ff4444"
    },
    "Lavender Mist": {
        "bg_primary": "#f8f4ff", "bg_secondary": "#ede6fa",
        "fg_primary": "#775599", "text_main": "#332244", "text_dim": "#887799",
        "waveform_fg": "#775599", "waveform_bg": "#f8f4ff", "accent_warn": "#cc6688",
        "btn_success": "#66aa66", "btn_danger": "#cc6666", "btn_text": "#ffffff",
        # Smart Cut Colors
        "skip_region": "#ffccdd", "skip_candidate": "#dd4466", "btn_skip": "#cc6666"
    },

    # =========================================================================
    # GROUP 3: VIBRANT & CREATIVE
    # =========================================================================
    "Synthwave": {
        "bg_primary": "#2b213a", "bg_secondary": "#241b30",
        "fg_primary": "#ff00ff", "text_main": "#00ffff", "text_dim": "#aa55aa",
        "waveform_fg": "#00ffff", "waveform_bg": "#2b213a", "accent_warn": "#ff9900",
        "btn_success": "#00ff00", "btn_danger": "#ff0066", "btn_text": "#ffffff",
        # Smart Cut Colors
        "skip_region": "#aa0044", "skip_candidate": "#ff0066", "btn_skip": "#ff0066"
    },
    "Cyberpunk": {
        "bg_primary": "#050505", "bg_secondary": "#101010",
        "fg_primary": "#fcee0a", "text_main": "#e0e0e0", "text_dim": "#555555",
        "waveform_fg": "#00ff99", "waveform_bg": "#050505", "accent_warn": "#ff0055",
        "btn_success": "#00ff00", "btn_danger": "#ff0000", "btn_text": "#000000",
        # Smart Cut Colors
        "skip_region": "#880000", "skip_candidate": "#ff0055", "btn_skip": "#ff0000"
    },
    "Dracula": {
        "bg_primary": "#282a36", "bg_secondary": "#44475a",
        "fg_primary": "#ff79c6", "text_main": "#f8f8f2", "text_dim": "#6272a4",
        "waveform_fg": "#bd93f9", "waveform_bg": "#282a36", "accent_warn": "#ff5555",
        "btn_success": "#50fa7b", "btn_danger": "#ff5555", "btn_text": "#ffffff",
        # Smart Cut Colors
        "skip_region": "#bd2c40", "skip_candidate": "#ff5555", "btn_skip": "#ff5555"
    },
    "Forest": {
        "bg_primary": "#1e2b1e", "bg_secondary": "#283828",
        "fg_primary": "#55aa55", "text_main": "#ccddcc", "text_dim": "#557755",
        "waveform_fg": "#55aa55", "waveform_bg": "#1e2b1e", "accent_warn": "#aa7755",
        "btn_success": "#448844", "btn_danger": "#884444", "btn_text": "#ffffff",
        # Smart Cut Colors
        "skip_region": "#552222", "skip_candidate": "#aa5555", "btn_skip": "#884444"
    },
    "Oceanic": {
        "bg_primary": "#002b36", "bg_secondary": "#073642",
        "fg_primary": "#2aa198", "text_main": "#93a1a1", "text_dim": "#586e75",
        "waveform_fg": "#2aa198", "waveform_bg": "#002b36", "accent_warn": "#d33682",
        "btn_success": "#859900", "btn_danger": "#dc322f", "btn_text": "#ffffff",
        # Smart Cut Colors
        "skip_region": "#882222", "skip_candidate": "#dc322f", "btn_skip": "#dc322f"
    },
    "Sunset": {
        "bg_primary": "#2d1b15", "bg_secondary": "#3d241c",
        "fg_primary": "#ff9933", "text_main": "#ffddcc", "text_dim": "#995544",
        "waveform_fg": "#ffcc33", "waveform_bg": "#2d1b15", "accent_warn": "#ff4444",
        "btn_success": "#88aa44", "btn_danger": "#cc4433", "btn_text": "#000000",
        # Smart Cut Colors
        "skip_region": "#883322", "skip_candidate": "#ff4444", "btn_skip": "#cc4433"
    },
    "Royal": {
        "bg_primary": "#220033", "bg_secondary": "#330044",
        "fg_primary": "#ffcc00", "text_main": "#eeddff", "text_dim": "#8855aa",
        "waveform_fg": "#ffcc00", "waveform_bg": "#220033", "accent_warn": "#cc0033",
        "btn_success": "#44cc44", "btn_danger": "#cc0000", "btn_text": "#000000",
        # Smart Cut Colors
        "skip_region": "#660022", "skip_candidate": "#ff0066", "btn_skip": "#cc0000"
    },
    "Bubblegum": {
        "bg_primary": "#222222", "bg_secondary": "#2a2a2a",
        "fg_primary": "#ff66aa", "text_main": "#ffffff", "text_dim": "#888888",
        "waveform_fg": "#ff66aa", "waveform_bg": "#222222", "accent_warn": "#ff3333",
        "btn_success": "#33cc99", "btn_danger": "#cc3333", "btn_text": "#ffffff",
        # Smart Cut Colors
        "skip_region": "#882244", "skip_candidate": "#ff3366", "btn_skip": "#cc3333"
    },
    "Toxic": {
        "bg_primary": "#111111", "bg_secondary": "#1a1a1a",
        "fg_primary": "#77ff00", "text_main": "#ccffcc", "text_dim": "#448844",
        "waveform_fg": "#77ff00", "waveform_bg": "#111111", "accent_warn": "#aa00aa",
        "btn_success": "#77ff00", "btn_danger": "#ff0000", "btn_text": "#000000",
        # Smart Cut Colors
        "skip_region": "#550000", "skip_candidate": "#ff0000", "btn_skip": "#ff0000"
    },
    "Monochrome Amber": {
        "bg_primary": "#111111", "bg_secondary": "#161616",
        "fg_primary": "#ffb000", "text_main": "#ffb000", "text_dim": "#664400",
        "waveform_fg": "#ffb000", "waveform_bg": "#111111", "accent_warn": "#ff5500",
        "btn_success": "#ffb000", "btn_danger": "#ff3300", "btn_text": "#000000",
        # Smart Cut Colors
        "skip_region": "#662200", "skip_candidate": "#ff5500", "btn_skip": "#ff3300"
    },

    # =========================================================================
    # GROUP 4: AESTHETIC
    # =========================================================================
    "Latte": {
        "bg_primary": "#f4ece4", "bg_secondary": "#e8ddd3",
        "fg_primary": "#6f4e37", "text_main": "#4a3b32", "text_dim": "#9c8c74",
        "waveform_fg": "#6f4e37", "waveform_bg": "#f4ece4", "accent_warn": "#a0522d",
        "btn_success": "#8fbc8f", "btn_danger": "#cd5c5c", "btn_text": "#ffffff",
        # Smart Cut Colors
        "skip_region": "#dcb4b4", "skip_candidate": "#cd5c5c", "btn_skip": "#cd5c5c"
    },
    "Sage & Sand": {
        "bg_primary": "#e3e8e3", "bg_secondary": "#d6ded6",
        "fg_primary": "#5e7a65", "text_main": "#2f3a32", "text_dim": "#7d8580",
        "waveform_fg": "#5e7a65", "waveform_bg": "#e3e8e3", "accent_warn": "#d2a679",
        "btn_success": "#556b2f", "btn_danger": "#8b4513", "btn_text": "#ffffff",
        # Smart Cut Colors
        "skip_region": "#cc9988", "skip_candidate": "#8b4513", "btn_skip": "#8b4513"
    },
    "Pastel Dream": {
        "bg_primary": "#fff5f5", "bg_secondary": "#f8e8ec",
        "fg_primary": "#6da0b8", "text_main": "#554455", "text_dim": "#aa99aa",
        "waveform_fg": "#ffb7b2", "waveform_bg": "#fff5f5", "accent_warn": "#ffdac1",
        "btn_success": "#77dd77", "btn_danger": "#ff6961", "btn_text": "#ffffff",
        # Smart Cut Colors
        "skip_region": "#ffd1d1", "skip_candidate": "#ff6961", "btn_skip": "#ff6961"
    },
    "Nordic": {
        "bg_primary": "#f0f4f8", "bg_secondary": "#d9e2ec",
        "fg_primary": "#486581", "text_main": "#102a43", "text_dim": "#627d98",
        "waveform_fg": "#334e68", "waveform_bg": "#f0f4f8", "accent_warn": "#9fb3c8",
        "btn_success": "#3e8e41", "btn_danger": "#b03030", "btn_text": "#ffffff",
        # Smart Cut Colors
        "skip_region": "#d0a0a0", "skip_candidate": "#b03030", "btn_skip": "#b03030"
    },
    "Terracotta": {
        "bg_primary": "#fcf5f2", "bg_secondary": "#f2e6e1",
        "fg_primary": "#c05640", "text_main": "#5d2e26", "text_dim": "#a88b85",
        "waveform_fg": "#c05640", "waveform_bg": "#fcf5f2", "accent_warn": "#d98a6c",
        "btn_success": "#556b2f", "btn_danger": "#8b4513", "btn_text": "#ffffff",
        # Smart Cut Colors
        "skip_region": "#ddaa99", "skip_candidate": "#8b4513", "btn_skip": "#8b4513"
    }
}