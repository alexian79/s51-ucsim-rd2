import ghidra.app.cmd.disassemble.DisassembleCommand;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.lang.Language;
import ghidra.program.model.lang.Register;
import ghidra.program.model.lang.RegisterValue;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.listing.InstructionIterator;
import ghidra.program.model.listing.ProgramContext;
import ghidra.program.model.mem.MemoryBlock;
import ghidra.program.model.pcode.PcodeOp;
import java.math.BigInteger;

/**
 * Asserts the dual-DPTR bank at an address and re-disassembles from there, for the cases the
 * language cannot resolve on its own.
 *
 * The RD2 language derives the bank while parsing, so any write whose value is not a constant -
 * `MOV AUXR1,A`, `MOV AUXR1,Rn`, `MOV AUXR1,dir` - leaves the parse-time bank unchanged and
 * Dptr51 reports the site. This script is the documented override: it sets the context register
 * explicitly and disassembles forward again, so DPTR operands resolve against the bank the code
 * actually runs with.
 *
 *   -scriptArgs "<addr> <0|1>"    force a bank from <addr> onward and re-disassemble
 *   -scriptArgs ""                report the current override and the bank each instruction used
 *
 * Determine the right value from the simulator, not from guesswork: t/dpstest.py and t/rmwtest.py
 * both read back which pointer a sequence selected, and t/uctrace.py traces AUXR1 per instruction.
 */
public class Bank51 extends GhidraScript {

    @Override
    public void run() throws Exception {
        Language lang = currentProgram.getLanguage();
        Register ctx = lang.getRegister("contextReg");
        if (ctx == null) {
            println("BANK51 this language has no contextReg - dual DPTR is not modelled here, "
                + "use AT89C51RD2:BE:16:default");
            return;
        }

        // analyzeHeadless script-argument plumbing varies between releases, so accept either
        // trailing tokens or environment variables; the harness uses the latter.
        String[] a = getScriptArgs();
        String argAddr = a.length > 0 ? a[0] : System.getenv("BANK51_ADDR");
        String argBank = a.length > 1 ? a[1] : System.getenv("BANK51_BANK");
        if (argAddr != null && argBank != null) {
            Address at = currentProgram.getImageBase().getAddressSpace()
                    .getAddress(Long.decode(argAddr).longValue());
            int bank = Integer.parseInt(argBank);
            if (bank != 0 && bank != 1) {
                println("BANK51 bank must be 0 or 1, got " + argBank);
                return;
            }
            // the range must stay inside one address space; getMaxAddress() is in BITS
            ghidra.program.model.mem.MemoryBlock blk = currentProgram.getMemory().getBlock(at);
            Address thru = blk == null ? at.getAddressSpace().getMaxAddress() : blk.getEnd();
            ProgramContext pc = currentProgram.getProgramContext();
            pc.setRegisterValue(at, thru, new RegisterValue(ctx, BigInteger.valueOf(bank)));
            println("BANK51 forced dps=" + bank + " from " + at);
            if (!runCommand(new DisassembleCommand(at, null, true))) {
                println("BANK51 re-disassembly failed at " + at);
            }
        }

        ProgramContext pc = currentProgram.getProgramContext();
        RegisterValue v = pc.getRegisterValue(ctx, currentProgram.getMinAddress());
        println("BANK51 override_at_entry " + (v == null ? "none" : v.toString()));

        MemoryBlock[] bs = currentProgram.getMemory().getBlocks();
        for (int i = 0; i < bs.length; i++) {
            if (bs[i].isExecute() && bs[i].isInitialized()
                    && currentProgram.getListing().getInstructionAt(bs[i].getStart()) == null) {
                runCommand(new DisassembleCommand(bs[i].getStart(), null, true));
            }
        }

        InstructionIterator it = currentProgram.getListing().getInstructions(true);
        while (it.hasNext()) {
            Instruction in = it.next();
            String bank = bankOf(in);
            if (bank != null) {
                println("  " + in.getAddress() + "  " + in.toString() + "   bank=" + bank);
            }
        }
    }

    /** Which pointer register this instruction actually touches, or null if it uses none. */
    private String bankOf(Instruction in) {
        String found = null;
        PcodeOp[] ops = in.getPcode();
        for (int i = 0; i < ops.length; i++) {
            for (int j = 0; j < 2; j++) {
                ghidra.program.model.pcode.Varnode vn =
                        j == 0 ? ops[i].getOutput() : (ops[i].getInputs().length > 0
                                ? ops[i].getInputs()[0] : null);
                if (vn == null || vn.isConstant()) {
                    continue;
                }
                Register r = currentProgram.getLanguage()
                        .getRegister(vn.getAddress(), vn.getSize());
                if (r == null) {
                    continue;
                }
                if (r.getName().equals("DPTR0") || r.getName().equals("DPTR1")) {
                    String b = r.getName().equals("DPTR1") ? "1" : "0";
                    if (found != null && !found.equals(b)) {
                        return "0+1";
                    }
                    found = b;
                }
            }
        }
        return found;
    }
}
