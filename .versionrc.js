const pyproject = {
  filename: "pyproject.toml",
  updater: {
    readVersion: (contents) => contents.match(/^version = "(.*)"$/m)[1],
    writeVersion: (contents, version) =>
      contents.replace(/^version = ".*"$/m, `version = "${version}"`),
  },
};

const serverVersion = {
  filename: "src/seshat/version.py",
  updater: {
    readVersion: (contents) => contents.match(/^SERVER_VERSION = "(.*)"$/m)[1],
    writeVersion: (contents, version) =>
      contents.replace(/^SERVER_VERSION = ".*"$/m, `SERVER_VERSION = "${version}"`),
  },
};

// Tooling-only releases are real releases. `build` is a hidden type in the
// conventional-commits preset, so a release that ships no behaviour would
// otherwise publish an empty changelog section; everything else keeps the
// preset's default visibility.
const types = [
  { type: "feat", section: "Features" },
  { type: "fix", section: "Bug Fixes" },
  { type: "perf", section: "Performance Improvements" },
  { type: "revert", section: "Reverts" },
  { type: "build", section: "Build System" },
  { type: "docs", section: "Documentation", hidden: true },
  { type: "style", section: "Styles", hidden: true },
  { type: "chore", section: "Miscellaneous Chores", hidden: true },
  { type: "refactor", section: "Code Refactoring", hidden: true },
  { type: "test", section: "Tests", hidden: true },
  { type: "ci", section: "Continuous Integration", hidden: true },
];

module.exports = {
  types,
  packageFiles: [pyproject],
  bumpFiles: [pyproject, serverVersion],
  tagPrefix: "v",
  releaseCommitMessageFormat: "chore(release): {{currentTag}}",
  commitUrlFormat: "https://github.com/blackopsrepl/seshat/commit/{{hash}}",
  compareUrlFormat:
    "https://github.com/blackopsrepl/seshat/compare/{{previousTag}}...{{currentTag}}",
};
