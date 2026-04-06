export function fmt(n: number | undefined) {
    if (n == null) return "—";
    if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(0) + "K";
    return n.toLocaleString();
}

export function qualityClass(score: number) {
    if (score >= 60) return "high";
    if (score >= 30) return "medium";
    return "low";
}

export function contactTypeIcon(type: string) {
    const icons: Record<string, string> = {
        email: "📧", phone: "📞", linkedin: "🔗", facebook: "🫂",
        instagram: "📸", twitter: "🐦", whatsapp: "💬", youtube: "▶️",
    };
    return icons[type] || "🌐";
}

let toastTimer: ReturnType<typeof setTimeout> | null = null;

export function showToast(msg: string, type: "copied" | "saved" | "skipped") {
    if (typeof document === "undefined") return;
    const existing = document.getElementById("tl-toast");
    if (existing) existing.remove();
    if (toastTimer) clearTimeout(toastTimer);
    const el = document.createElement("div");
    el.id = "tl-toast";
    el.className = `toast ${type}`;
    el.textContent = msg;
    document.body.appendChild(el);
    toastTimer = setTimeout(() => el.remove(), 1800);
}

export function copyText(text: string) {
    if (typeof navigator !== "undefined") {
        navigator.clipboard.writeText(text).catch(() => { });
        showToast(`Copied: ${text.slice(0, 30)}`, "copied");
    }
}
