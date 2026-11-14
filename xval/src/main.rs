// Cross-validate an 8051 program on the i8051 core inside mcs51-rs.
//
// Accepts either an .asm source (assembled and linked here) or a prebuilt
// .ihx/.hex image, so the identical bytes can be diffed against ucsim's 89C51R
// model. Prints the pre-execution state of every retired instruction.
use mcs51_asm::{assemble, Options};
use mcs51_ihex::Image;
use mcs51_ld::LinkOptions;
use mcs51_sim::{Machine, StopReason};

fn main() -> std::process::ExitCode {
    let args: Vec<String> = std::env::args().collect();
    let path = match args.get(1) {
        Some(p) => p.clone(),
        None => {
            eprintln!("usage: xval <file.asm|file.ihx> [max_instructions] [out.ihx]");
            return std::process::ExitCode::FAILURE;
        }
    };
    let max: u64 = args.get(2).and_then(|s| s.parse().ok()).unwrap_or(4000);

    let image = if path.ends_with(".ihx") || path.ends_with(".hex") {
        match std::fs::read_to_string(&path) {
            Ok(t) => match Image::from_intel_hex(&t) {
                Ok(i) => i,
                Err(e) => {
                    eprintln!("parse {path}: {e}");
                    return std::process::ExitCode::FAILURE;
                }
            },
            Err(e) => {
                eprintln!("read {path}: {e}");
                return std::process::ExitCode::FAILURE;
            }
        }
    } else {
        let src = match std::fs::read_to_string(&path) {
            Ok(s) => s,
            Err(e) => {
                eprintln!("read {path}: {e}");
                return std::process::ExitCode::FAILURE;
            }
        };
        let asm = assemble(&src, &Options::new(path.clone()));
        if !asm.is_ok() {
            eprint!("{}", asm.render_diagnostics());
            return std::process::ExitCode::FAILURE;
        }
        let linked = mcs51_ld::link(&[asm.object], &LinkOptions::default());
        if !linked.is_ok() {
            eprint!("{}", linked.render_diagnostics());
            return std::process::ExitCode::FAILURE;
        }
        linked.code
    };

    if let Some(o) = args.get(3) {
        std::fs::write(o, image.to_intel_hex()).expect("write image");
    }

    let mut m = Machine::from_image(&image);
    let mut steps = 0u64;
    let stop = loop {
        if steps >= max {
            break StopReason::StepLimit;
        }
        let s = m.snapshot();
        println!("T {:04x} {:>8} {:02x} {:02x}", s.pc, s.cycles, s.a, s.psw);
        if !m.step() {
            break StopReason::Halted;
        }
        steps += 1;
    };

    let s = m.snapshot();
    println!(
        "S pc={:04x} a={:02x} b={:02x} dptr={:04x} sp={:02x} psw={:02x} cycles={} inst={} stop={:?}",
        s.pc, s.a, s.b, s.dptr, s.sp, s.psw, s.cycles, s.instructions, stop
    );
    std::process::ExitCode::SUCCESS
}
