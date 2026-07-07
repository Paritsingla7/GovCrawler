document.getElementById("login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const email = document.getElementById("login-email").value.trim();
    const password = document.getElementById("login-password").value;
    const errorEl = document.getElementById("login-error");
    const submitBtn = document.getElementById("login-submit");

    errorEl.style.display = "none";
    submitBtn.disabled = true;

    try {
        const res = await fetch("/auth/login", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({email, password}),
        });
        if (!res.ok) {
            const body = await res.json().catch(() => ({}));
            errorEl.textContent = body.detail || "Login failed.";
            errorEl.style.display = "block";
            return;
        }
        window.location.href = "/";
    } catch (err) {
        errorEl.textContent = "Could not reach the server.";
        errorEl.style.display = "block";
    } finally {
        submitBtn.disabled = false;
    }
});
