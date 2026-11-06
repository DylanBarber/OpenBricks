"use client";

import { useEffect, useState } from "react";
import { LogIn } from "lucide-react";
import { api } from "@/lib/api";
import styles from "./security-panels.module.css";

type Provider = { id: string; name: string; login_url: string };

export function OidcLogin() {
  const [providers, setProviders] = useState<Provider[]>([]);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let active = true;
    setFailed(
      new URLSearchParams(window.location.search).get("sso_error") ===
        "login_failed",
    );
    void api<{ providers: Provider[] }>("/auth/providers")
      .then((value) => {
        if (active)
          setProviders(
            value.providers.filter(
              (item) =>
                item.id === "oidc" && item.login_url === "/api/auth/oidc/login",
            ),
          );
      })
      .catch(() => {
        /* Local login remains usable when SSO is unavailable. */
      });
    return () => {
      active = false;
    };
  }, []);
  return (
    <div className={styles.login}>
      {failed ? (
        <p className={styles.error} role="alert">
          Single sign-on could not complete. Try again or use your local
          account.
        </p>
      ) : null}
      {providers.map((provider) => (
        <a
          className="button"
          key={provider.id}
          href={`${provider.login_url}?return_to=%2F`}
        >
          <LogIn size={16} />
          Continue with {provider.name}
        </a>
      ))}
    </div>
  );
}
