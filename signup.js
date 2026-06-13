// MikeCast email-newsletter signup — shared by index.html and subscribe.html.
//
// SETUP: after deploying the newsletter-signup Lambda, paste its Function URL
// below (no trailing slash). The handler exposes POST /signup.
const MIKECAST_SIGNUP_ENDPOINT = "https://REPLACE-WITH-LAMBDA-FUNCTION-URL";

(function () {
  const form = document.getElementById("email-signup");
  const status = document.getElementById("signup-status");
  if (!form || !status) return;

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const email = (form.email.value || "").trim();
    if (!email) return;

    const button = form.querySelector("button");
    button.disabled = true;
    status.textContent = "Subscribing…";
    status.className = "signup-status";

    try {
      const resp = await fetch(MIKECAST_SIGNUP_ENDPOINT + "/signup", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email }),
      });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      // Never reveal whether the address already existed — always the same copy.
      status.textContent = "Check your inbox to confirm your subscription.";
      status.className = "signup-status ok";
      form.reset();
    } catch (err) {
      status.textContent = "Something went wrong — please try again.";
      status.className = "signup-status err";
    } finally {
      button.disabled = false;
    }
  });
})();
