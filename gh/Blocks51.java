import ghidra.app.cmd.disassemble.DisassembleCommand;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.mem.MemoryBlock;

/** Lists memory blocks and probes whether external-data addresses have any coverage. */
public class Blocks51 extends GhidraScript {
    @Override
    public void run() throws Exception {
        MemoryBlock[] bs = currentProgram.getMemory().getBlocks();
        for (int i = 0; i < bs.length; i++) {
            println("BLOCK " + bs[i].getName() + " " + bs[i].getStart() + ".." + bs[i].getEnd()
                + " len=" + bs[i].getSize() + " init=" + bs[i].isInitialized());
        }
        String[] probe = { "EXTMEM:0100", "EXTMEM:06ff", "EXTMEM:0700", "EXTMEM:1234" };
        for (int i = 0; i < probe.length; i++) {
            Address a = currentProgram.getAddressFactory().getAddress(probe[i]);
            MemoryBlock b = currentProgram.getMemory().getBlock(a);
            println("PROBE " + probe[i] + " block=" + (b == null ? "none" : b.getName()));
        }
        for (int i = 0; i < bs.length; i++) {
            if (bs[i].isExecute() && bs[i].isInitialized()
                    && currentProgram.getListing().getInstructionAt(bs[i].getStart()) == null) {
                runCommand(new DisassembleCommand(bs[i].getStart(), null, true));
            }
        }
    }
}
