//! Ephemeral per-run RS256 signer + JWKS (IAM-04, D-08).
//!
//! A fresh RSA-2048 keypair is generated **in-memory at startup** (never written
//! to disk) and held in `AppState` behind an `Arc`, so token-mint, the JWKS
//! endpoint, and (Plan 10-04) the `--enforce-auth` validation all share the one
//! run-scoped key. The JWKS serves the public half; the JWT header `kid` is set to
//! the same per-run id as the single JWK entry so a consumer can match them
//! (`Jwk::from_encoding_key` returns `kid = None` — we set both sides ourselves).
//!
//! SECURITY — rsa 0.9 / RUSTSEC-2023-0071 ("Marvin" timing side-channel): the
//! advisory is a decryption-oracle attack. It recovers a private key by timing RSA
//! **decryption** across many attacker-chosen ciphertexts. This module only
//! **signs** and **verifies** (RS256) — there is no code path that accepts a
//! ciphertext and decrypts it, so the oracle the attack needs does not exist. The
//! key is also ephemeral per-run: generated in memory at startup, never persisted,
//! and protecting nothing beyond this process's own mock tokens.
//!
//! Do NOT extend this rationale to network exposure. The server defaults to a
//! 127.0.0.1 bind, but the container image sets `HOST=0.0.0.0` (see
//! docker-compose.yml), so it is not loopback-only and must not be assumed to be.
//! The exception rests solely on the absent decryption path.
//!
//! rsa 0.9.10 is the current release; do NOT chase rsa 0.10.0-rc.x (release
//! candidate). The matching cargo-audit exception lives in `.cargo/audit.toml`;
//! keep the two rationales in sync, and drop both when 0.10 ships.

use jsonwebtoken::jwk::{Jwk, JwkSet, PublicKeyUse};
use jsonwebtoken::{Algorithm, DecodingKey, EncodingKey, Header, encode};
use rsa::RsaPrivateKey;
use rsa::pkcs8::{EncodePrivateKey, EncodePublicKey, LineEnding};
use std::sync::{Arc, RwLock};

/// The v1.0 ARM issuer for a served tenant — `https://sts.windows.net/{tenant_id}/`
/// (RESEARCH Q2). The SINGLE source of truth for the issuer format: [`JwtSigner::ephemeral`]
/// builds the signer's `issuer` from it, and the hot-swap change-guard
/// ([`SharedSigner::serves_tenant`]) compares against it — so the two can never drift.
pub fn issuer_for(tenant_id: &uuid::Uuid) -> String {
    format!("https://sts.windows.net/{tenant_id}/")
}

/// The run-scoped RS256 signer: the in-memory keypair, its JWKS export, the
/// per-run `kid`, and the v1.0 ARM `iss`/`aud` bound to the served tenant.
pub struct JwtSigner {
    /// Signing key (mint).
    pub encoding: EncodingKey,
    /// Verifying key (Plan 10-04 `--enforce-auth` validation).
    pub decoding: DecodingKey,
    /// Public half exported at `GET /{tenant}/discovery/v2.0/keys`.
    pub jwks: JwkSet,
    /// Per-run key id — set on BOTH the JWT header and the single JWK entry.
    pub kid: String,
    /// v1.0 ARM issuer `https://sts.windows.net/{tenant_id}/` (RESEARCH Q2).
    pub issuer: String,
    /// Canonical ARM audience `https://management.azure.com/` (trailing slash).
    pub audience: String,
}

impl JwtSigner {
    /// Generate a fresh ephemeral RSA-2048 key at startup (D-08). Never on disk.
    pub fn ephemeral(
        tenant_id: &uuid::Uuid,
    ) -> Result<Self, Box<dyn std::error::Error + Send + Sync>> {
        // Ephemeral RSA-2048 keypair → PKCS#8 PEM (in-memory only, never written
        // to disk). OsRng is a CryptoRng satisfying rsa 0.9's rand_core 0.6 bound.
        let mut rng = rand::rngs::OsRng;
        let priv_key = RsaPrivateKey::new(&mut rng, 2048)?;
        let pem = priv_key.to_pkcs8_pem(LineEnding::LF)?; // "PRIVATE KEY" PKCS#8
        let pem_bytes = pem.as_bytes();

        let encoding = EncodingKey::from_rsa_pem(pem_bytes)?;
        // The VERIFYING key MUST be built from the SPKI PUBLIC-key PEM, not the private
        // PKCS#8 PEM: `DecodingKey::from_rsa_pem` parses a `BEGIN PUBLIC KEY` document
        // (SubjectPublicKeyInfo). Feeding it the private PEM yields a key that fails to
        // verify every signature — so `--enforce-auth` would 401 even a freshly minted
        // token (Plan 10-04 surfaced this; 10-02's round-trip only exercised the JWKS
        // path). Derive the public half explicitly.
        let public_pem = priv_key.to_public_key().to_public_key_pem(LineEnding::LF)?;
        let decoding = DecodingKey::from_rsa_pem(public_pem.as_bytes())?;

        // Per-run key id — any stable per-run string; the sim has exactly one key.
        let kid = uuid::Uuid::new_v4().to_string();
        let mut jwk = Jwk::from_encoding_key(&encoding, Algorithm::RS256)?;
        // `from_encoding_key` leaves kid = None; link header.kid ↔ JWKS entry.
        jwk.common.key_id = Some(kid.clone());
        jwk.common.public_key_use = Some(PublicKeyUse::Signature);
        let jwks = JwkSet { keys: vec![jwk] };

        // v1.0 ARM identity tied to the served tenant (RESEARCH Q2).
        let issuer = issuer_for(tenant_id);
        let audience = "https://management.azure.com/".to_string();

        Ok(Self {
            encoding,
            decoding,
            jwks,
            kid,
            issuer,
            audience,
        })
    }

    /// Mint a v1.0-shaped ARM JWT, setting `header.kid` to the per-run `kid`.
    pub fn mint(&self, claims: &impl serde::Serialize) -> jsonwebtoken::errors::Result<String> {
        let mut header = Header::new(Algorithm::RS256);
        header.kid = Some(self.kid.clone()); // matches the single JWKS entry
        encode(&header, claims, &self.encoding)
    }
}

/// A hot-swappable handle to the run's [`JwtSigner`], shared by `AppState` and the
/// control-plane job runner ([`crate::job::ControlPlane`]).
///
/// Reads are frequent and tiny — every `--enforce-auth` request validates against it, and
/// every `/token`, JWKS, and OIDC-discovery call reads it. Writes are extremely rare: only a
/// tenant-mutating control job (generate / restore / reset) rebuilds it, once, on success. A
/// `RwLock<Arc<JwtSigner>>` fits that read-heavy / write-rare shape without a new dependency.
///
/// The contract (kept centralized here so handlers never scatter `.read().unwrap()`):
///   * [`load`](Self::load) clones the `Arc` under a briefly-held READ lock and releases it
///     immediately — callers hold the returned `Arc`, never the lock, so nothing is held
///     across an `.await`.
///   * [`store`](Self::store) takes the WRITE lock ONLY to swap the pointer; the caller
///     builds the whole new signer OUTSIDE the lock first.
///   * Lock poisoning is recovered here (`into_inner`): the only critical sections clone or
///     replace an `Arc`, so a poisoned lock still guards a valid signer — there is no torn
///     state to propagate.
#[derive(Clone)]
pub struct SharedSigner(Arc<RwLock<Arc<JwtSigner>>>);

impl SharedSigner {
    /// Wrap an initial signer.
    pub fn new(signer: JwtSigner) -> Self {
        Self(Arc::new(RwLock::new(Arc::new(signer))))
    }

    /// The current signer: clone the `Arc` under a short read lock, release immediately.
    pub fn load(&self) -> Arc<JwtSigner> {
        self.0.read().unwrap_or_else(|p| p.into_inner()).clone()
    }

    /// Replace the signer with `next` (built by the caller OUTSIDE the lock). Only the
    /// pointer swap runs under the write lock.
    pub fn store(&self, next: JwtSigner) {
        let mut guard = self.0.write().unwrap_or_else(|p| p.into_inner());
        *guard = Arc::new(next);
    }

    /// True iff the current signer already serves `tenant_id` (its issuer matches). The
    /// change-guard: a refresh rebuilds ONLY when this is false, so a mutation that leaves
    /// the effective tenant unchanged never rotates the key or invalidates live tokens.
    pub fn serves_tenant(&self, tenant_id: &uuid::Uuid) -> bool {
        self.load().issuer == issuer_for(tenant_id)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use jsonwebtoken::jwk::{AlgorithmParameters, KeyAlgorithm, PublicKeyUse};
    use jsonwebtoken::{Algorithm, DecodingKey, Validation, decode, decode_header};
    use serde::{Deserialize, Serialize};

    #[derive(Debug, Serialize, Deserialize)]
    struct TestClaims {
        iss: String,
        aud: String,
        tid: String,
        sub: String,
        exp: usize,
    }

    fn fixture_tenant() -> uuid::Uuid {
        uuid::Uuid::nil()
    }

    fn future_exp() -> usize {
        use std::time::{SystemTime, UNIX_EPOCH};
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs();
        (now + 3600) as usize
    }

    fn claims_for(signer: &JwtSigner, tid: &uuid::Uuid) -> TestClaims {
        TestClaims {
            iss: signer.issuer.clone(),
            aud: signer.audience.clone(),
            tid: tid.to_string(),
            sub: "spn-test".to_string(),
            exp: future_exp(),
        }
    }

    /// GIVEN an ephemeral signer, WHEN it mints a claims set and we decode the token
    /// AGAINST the public key in the signer's own JWKS (n/e), THEN decode succeeds and
    /// the round-tripped iss/aud/tid match what was minted.
    #[test]
    fn mint_decode_roundtrip() {
        let tid = fixture_tenant();
        let signer = JwtSigner::ephemeral(&tid).expect("ephemeral signer");
        let claims = claims_for(&signer, &tid);
        let token = signer.mint(&claims).expect("mint");

        // Decode against the SERVED JWKS entry, proving the public key round-trips.
        let jwk = &signer.jwks.keys[0];
        let decoding = DecodingKey::from_jwk(jwk).expect("decoding key from jwk");
        let mut validation = Validation::new(Algorithm::RS256);
        validation.set_issuer(std::slice::from_ref(&signer.issuer));
        validation.set_audience(std::slice::from_ref(&signer.audience));
        validation.validate_exp = true;

        let data = decode::<TestClaims>(&token, &decoding, &validation).expect("decode via jwks");
        assert_eq!(data.claims.iss, signer.issuer);
        assert_eq!(data.claims.aud, signer.audience);
        assert_eq!(data.claims.tid, tid.to_string());
    }

    /// GIVEN a minted token, WHEN it is decoded against the signer's OWN `decoding`
    /// key (the `--enforce-auth` verification path, NOT the JWKS path), THEN it
    /// validates — `decoding` must be the public half, so the enforce branch accepts a
    /// freshly minted token (Plan 10-04 regression: a private-PEM `decoding` 401s all).
    #[test]
    fn mint_decode_via_signer_decoding() {
        let tid = fixture_tenant();
        let signer = JwtSigner::ephemeral(&tid).expect("ephemeral signer");
        let token = signer.mint(&claims_for(&signer, &tid)).expect("mint");

        let mut validation = Validation::new(Algorithm::RS256);
        validation.set_issuer(std::slice::from_ref(&signer.issuer));
        validation.set_audience(std::slice::from_ref(&signer.audience));
        validation.validate_exp = true;

        decode::<TestClaims>(&token, &signer.decoding, &validation)
            .expect("token must validate against signer.decoding (the enforce path)");
    }

    /// GIVEN a minted token, THEN its header `kid` equals the single JWKS entry's
    /// `kid` (Some), so a consumer can select the right key.
    #[test]
    fn header_kid_matches_jwks() {
        let tid = fixture_tenant();
        let signer = JwtSigner::ephemeral(&tid).expect("signer");
        let token = signer.mint(&claims_for(&signer, &tid)).expect("mint");

        let header = decode_header(&token).expect("decode header");
        let jwk_kid = signer.jwks.keys[0].common.key_id.clone();
        assert!(jwk_kid.is_some(), "JWKS entry must carry a kid");
        assert_eq!(header.kid, jwk_kid, "header.kid must match the JWKS entry");
    }

    /// GIVEN two `ephemeral()` calls, THEN their kids AND public keys differ — the
    /// key is regenerated per run (D-08).
    #[test]
    fn two_signers_differ() {
        let a = JwtSigner::ephemeral(&fixture_tenant()).expect("signer a");
        let b = JwtSigner::ephemeral(&fixture_tenant()).expect("signer b");
        assert_ne!(a.kid, b.kid, "per-run kids must differ");

        let n = |s: &JwtSigner| match &s.jwks.keys[0].algorithm {
            AlgorithmParameters::RSA(p) => p.n.clone(),
            _ => panic!("JWK is not RSA"),
        };
        assert_ne!(n(&a), n(&b), "per-run public moduli must differ (D-08)");
    }

    /// A `SharedSigner` reads back the signer it was built with, and `serves_tenant`
    /// distinguishes the served tenant from any other.
    #[test]
    fn shared_signer_load_and_serves_tenant() {
        let tid_a = uuid::Uuid::from_u128(0xA);
        let tid_b = uuid::Uuid::from_u128(0xB);
        let shared = SharedSigner::new(JwtSigner::ephemeral(&tid_a).expect("signer a"));

        assert_eq!(shared.load().issuer, issuer_for(&tid_a));
        assert!(
            shared.serves_tenant(&tid_a),
            "serves the tenant it was built for"
        );
        assert!(
            !shared.serves_tenant(&tid_b),
            "does not serve a different tenant"
        );
    }

    /// `store` swaps the WHOLE signer atomically: the issuer moves to the new tenant AND the
    /// per-run key (kid) rotates (full identity epoch). A clone of the handle observes the
    /// swap (shared state).
    #[test]
    fn shared_signer_store_swaps_identity_and_rotates_key() {
        let tid_a = uuid::Uuid::from_u128(0xA);
        let tid_b = uuid::Uuid::from_u128(0xB);
        let shared = SharedSigner::new(JwtSigner::ephemeral(&tid_a).expect("signer a"));
        let handle = shared.clone(); // a second holder (as AppState + ControlPlane share one)

        let before_kid = shared.load().kid.clone();
        shared.store(JwtSigner::ephemeral(&tid_b).expect("signer b"));

        let after = handle.load(); // observed through the CLONE
        assert_eq!(
            after.issuer,
            issuer_for(&tid_b),
            "issuer moved to the new tenant"
        );
        assert_ne!(
            after.kid, before_kid,
            "the per-run key (kid) rotated on swap"
        );
        assert!(handle.serves_tenant(&tid_b) && !handle.serves_tenant(&tid_a));
    }

    /// GIVEN the JWKS, THEN its single entry advertises RS256 + use=sig.
    #[test]
    fn alg_is_rs256() {
        let signer = JwtSigner::ephemeral(&fixture_tenant()).expect("signer");
        let jwk = &signer.jwks.keys[0];
        assert_eq!(
            jwk.common.public_key_use,
            Some(PublicKeyUse::Signature),
            "JWK use must be Signature"
        );
        assert_eq!(
            jwk.common.key_algorithm,
            Some(KeyAlgorithm::RS256),
            "JWK alg must be RS256"
        );
        assert!(
            matches!(jwk.algorithm, AlgorithmParameters::RSA(_)),
            "JWK key params must be RSA"
        );
    }
}
