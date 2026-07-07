fn main() {
    println!("cargo:rerun-if-env-changed=AGENTOS_BUILD_CHANNEL");
    let channel = std::env::var("AGENTOS_BUILD_CHANNEL").unwrap_or_else(|_| "dev".to_string());
    match channel.as_str() {
        "release" | "dev" => {}
        other => panic!("AGENTOS_BUILD_CHANNEL must be `release` or `dev`, got `{other}`"),
    }
    println!("cargo:rustc-env=AGENTOS_BUILD_CHANNEL={channel}");
}
