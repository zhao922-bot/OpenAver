/** Return a path string from a legacy string or a DirectoryConfig object. */
export function dirPath(directory) {
    return typeof directory === 'string' ? directory : (directory?.path ?? '');
}
