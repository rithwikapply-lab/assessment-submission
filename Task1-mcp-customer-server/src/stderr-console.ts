import util from "node:util";

console.log = (...args: unknown[]) => {
  process.stderr.write(util.format(...args) + "\n");
};
console.info = (...args: unknown[]) => {
  process.stderr.write(util.format(...args) + "\n");
};
console.debug = (...args: unknown[]) => {
  process.stderr.write(util.format(...args) + "\n");
};
console.warn = (...args: unknown[]) => {
  process.stderr.write(util.format(...args) + "\n");
};
