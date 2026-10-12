/*
 * Copyright (c) 2026 OceanBase.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 * http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#[cfg(windows)]
#[tokio::main]
async fn main() {
    use powercontext_desktop::{
        diagnostics::{DiagnosticKind, LocalDiagnostics},
        error::SafeError,
    };
    let path = std::path::PathBuf::from(
        std::env::args_os()
            .nth(1)
            .expect("isolated fixture registration"),
    );
    let registration: serde_json::Value =
        serde_json::from_slice(&std::fs::read(&path).expect("read isolated registration"))
            .expect("parse isolated registration");
    let invalid_directory = tempfile::tempdir().expect("isolated invalid registrations");
    for (field, mismatch) in [
        (
            "version",
            format!("{}-mismatch", registration["version"].as_str().unwrap()),
        ),
        ("sha256", "0".repeat(64)),
    ] {
        let mut invalid = registration.clone();
        invalid[field] = mismatch.into();
        let invalid_path = invalid_directory.path().join(format!("{field}.json"));
        std::fs::write(&invalid_path, serde_json::to_vec(&invalid).unwrap()).unwrap();
        assert_eq!(
            LocalDiagnostics::new(invalid_path)
                .run(DiagnosticKind::Service)
                .await
                .unwrap_err(),
            SafeError::CompatibilityUnverified,
            "mismatched {field} must remain rejected"
        );
    }
    let diagnostics = LocalDiagnostics::new(path);
    let service = diagnostics
        .run(DiagnosticKind::Service)
        .await
        .expect("real service status projection");
    let integrations = diagnostics
        .run(DiagnosticKind::Integrations)
        .await
        .expect("real integrations projection");
    assert!(
        service
            .items
            .iter()
            .any(|v| v.field == "registration" && v.status == "not_installed")
    );
    assert_eq!(integrations.hosts.len(), 8);
    println!(
        "{}",
        serde_json::json!({"service":service,"integrations":integrations,
            "registrationChecks":{"versionMismatchRejected":true,"digestMismatchRejected":true}})
    );
}
#[cfg(not(windows))]
fn main() {
    panic!("Windows qualification required");
}
