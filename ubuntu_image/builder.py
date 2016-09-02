"""Flow for building a disk image."""


import os
import shutil
import logging

from contextlib import ExitStack, contextmanager
from math import ceil
from operator import attrgetter
from tempfile import TemporaryDirectory
from ubuntu_image.helpers import MiB, run, snap
from ubuntu_image.image import Image, MBRImage
from ubuntu_image.parser import BootLoader, FileSystemType,\
                                VolumeSchema, parse as parse_yaml
from ubuntu_image.state import State


SPACE = ' '
_logger = logging.getLogger('ubuntu-image')


@contextmanager
def mount(img):
    with ExitStack() as resources:                  # pragma: notravis
        tmpdir = resources.enter_context(TemporaryDirectory())
        mountpoint = os.path.join(tmpdir, 'root-mount')
        os.makedirs(mountpoint)
        run('sudo mount -oloop {} {}'.format(img, mountpoint))
        resources.callback(run, 'sudo umount {}'.format(mountpoint))
        yield mountpoint


def _mkfs_ext4(img_file, contents_dir, label='writable'):
    """Encapsulate the `mkfs.ext4` invocation.

    As of e2fsprogs 1.43.1, mkfs.ext4 supports a -d option which allows
    you to populate the ext4 partition at creation time, with the
    contents of an existing directory.  Unfortunately, we're targeting
    Ubuntu 16.04, which has e2fsprogs 1.42.X without the -d flag.  In
    that case, we have to sudo loop mount the ext4 file system and
    populate it that way.  Which sucks because sudo.
    """
    cmd = 'mkfs.ext4 -L {} -O -metadata_csum {} -d {}'.format(
        label, img_file, contents_dir)
    proc = run(cmd, check=False)
    if proc.returncode == 0:                           # pragma: notravis
        # We have a new enough e2fsprogs, so we're done.
        return
    run('mkfs.ext4 -L {} {}'.format(label, img_file))  # pragma: notravis
    with mount(img_file) as mountpoint:                # pragma: notravis
        # fixme: everything is terrible.
        run('sudo cp -dR --preserve=mode,timestamps {}/* {}'.format(
            contents_dir, mountpoint), shell=True)


class ModelAssertionBuilder(State):
    def __init__(self, args):
        super().__init__()
        # The working directory will contain several bits as we stitch
        # everything together.  It will contain the final disk image file
        # (unless output is given).  It will contain an unpack/ directory
        # which is where `snap prepare-image` will put its contents.  It will
        # contain a system-data/ directory which containing everything needed
        # for the final root file system (e.g. an empty boot/ mount point, the
        # snap/ directory and a var/ hierarchy containing snaps and
        # sideinfos), and it will contain a boot/ directory with the grub
        # files.
        self.workdir = (
            self.resources.enter_context(TemporaryDirectory())
            if args.workdir is None
            else args.workdir)
        # Where the disk.img file ends up.
        self.output = (
            os.path.join(self.workdir, 'disk.img')
            if args.output is None
            else args.output)
        # Information passed between states.
        self.rootfs = None
        self.rootfs_size = 0
        self.bootfs = None
        self.bootfs_sizes = None
        self.images = None
        self.boot_images = None
        self.root_img = None
        self.disk_img = None
        self.gadget = None
        self.args = args
        self.unpackdir = None
        self.cloud_init = args.cloud_init
        self._next.append(self.make_temporary_directories)

    def __getstate__(self):
        state = super().__getstate__()
        state.update(
            args=self.args,
            boot_images=self.boot_images,
            bootfs=self.bootfs,
            bootfs_sizes=self.bootfs_sizes,
            disk_img=self.disk_img,
            gadget=self.gadget,
            images=self.images,
            output=self.output,
            root_img=self.root_img,
            rootfs=self.rootfs,
            rootfs_size=self.rootfs_size,
            unpackdir=self.unpackdir,
            cloud_init=self.cloud_init,
            )
        return state

    def __setstate__(self, state):
        super().__setstate__(state)
        self.args = state['args']
        self.boot_images = state['boot_images']
        self.bootfs = state['bootfs']
        self.bootfs_sizes = state['bootfs_sizes']
        self.disk_img = state['disk_img']
        self.gadget = state['gadget']
        self.images = state['images']
        self.output = state['output']
        self.root_img = state['root_img']
        self.rootfs = state['rootfs']
        self.rootfs_size = state['rootfs_size']
        self.unpackdir = state['unpackdir']
        self.cloud_init = state['cloud_init']

    def make_temporary_directories(self):
        self.rootfs = os.path.join(self.workdir, 'root')
        self.unpackdir = os.path.join(self.workdir, 'unpack')
        os.makedirs(self.rootfs)
        # Despite the documentation, `snap prepare-image` doesn't create the
        # gadget/ directory.
        os.makedirs(os.path.join(self.unpackdir, 'gadget'))
        self._next.append(self.prepare_image)

    def prepare_image(self):
        # Run `snap prepare-image` on the model.assertion.  sudo is currently
        # required in all cases, but eventually, it won't be necessary at
        # least for UEFI support.
        snap(self.args.model_assertion, self.unpackdir, self.args.channel, self.args.extra_snaps)
        self._next.append(self.load_gadget_yaml)

    def load_gadget_yaml(self):
        yaml_file = os.path.join(
            self.unpackdir, 'gadget', 'meta', 'gadget.yaml')
        with open(yaml_file, 'r', encoding='utf-8') as fp:
            self.gadget = parse_yaml(fp)
        self._next.append(self.populate_rootfs_contents)

    def populate_rootfs_contents(self):
        src = os.path.join(self.unpackdir, 'image')
        dst = os.path.join(self.rootfs, 'system-data')
        shutil.move(os.path.join(src, 'var'), os.path.join(dst, 'var'))
        seed_dir = os.path.join(dst, 'var', 'lib', 'cloud', 'seed')
        cloud_dir = os.path.join(seed_dir, 'nocloud-net')
        os.makedirs(cloud_dir, exist_ok=True)
        metadata_file = os.path.join(cloud_dir, 'meta-data')
        with open(metadata_file, 'w', encoding='utf-8') as fp:
            print('instance-id: nocloud-static', file=fp)
        if self.cloud_init is not None:
            userdata_file = os.path.join(cloud_dir, 'user-data')
            shutil.copy(self.cloud_init, userdata_file)
        # This is just a mount point.
        os.makedirs(os.path.join(dst, 'boot'))
        self._next.append(self.calculate_rootfs_size)

    def _calculate_dirsize(self, path):
        total = 0
        for dirpath, dirnames, filenames in os.walk(path):
            for filename in filenames:              # pragma: notravis
                total += os.path.getsize(os.path.join(dirpath, filename))
        # Fudge factor for incidentals.
        total *= 1.5
        return total

    def calculate_rootfs_size(self):
        # Calculate the size of the root file system.  Basically, I'm trying
        # to reproduce du(1) close enough without having to call out to it and
        # parse its output.
        self.rootfs_size = self._calculate_dirsize(self.rootfs)
        self._next.append(self.pre_populate_bootfs_contents)

    def pre_populate_bootfs_contents(self):
        volumes = self.gadget.volumes.values()
        assert len(volumes) == 1, 'For now, only one volume is allowed'
        volume = list(volumes)[0]
        for partnum, part in enumerate(volume.structures):
            target_dir = os.path.join(self.workdir, 'part{}'.format(partnum))
            os.makedirs(target_dir, exist_ok=True)
        self._next.append(self.populate_bootfs_contents)

    def populate_bootfs_contents(self):             # pragma: notravis
        # The unpack directory has a boot/ directory inside it.  The contents
        # of this directory (but not the parent <unpack>/boot directory
        # itself) needs to be moved to the bootfs directory.

        volume = list(self.gadget.volumes.values())[0]
        # At least one structure is required.
        for partnum, part in enumerate(volume.structures):
            target_dir = os.path.join(self.workdir, 'part{}'.format(partnum))
            # XXX: Use fs label for the moment, until we get a proper way to
            # identify the boot partition.
            if part.filesystem_label == 'system-boot':
                self.bootfs = target_dir
                if volume.bootloader is BootLoader.uboot:
                    boot = os.path.join(self.unpackdir, 'image', 'boot',
                                        'uboot')
                    ubuntu = target_dir
                elif volume.bootloader is BootLoader.grub:
                    boot = os.path.join(self.unpackdir, 'image', 'boot',
                                        'grub')
                    # XXX: Bad special-casing.  `snap prepare-image` currently
                    # installs to /boot/grub, but we need to map this to
                    # /EFI/ubuntu.  This is because we are using a SecureBoot
                    # signed bootloader image which has this path embedded, so
                    # we need to install our files to there.
                    ubuntu = os.path.join(target_dir, 'EFI', 'ubuntu')
                else:
                    raise ValueError("unsupported bootloader value {}"
                                     .format(volume.bootloader))
                os.makedirs(ubuntu, exist_ok=True)
                for filename in os.listdir(boot):
                    src = os.path.join(boot, filename)
                    dst = os.path.join(ubuntu, filename)
                    shutil.move(src, dst)
            if part.filesystem is not FileSystemType.none:
                for file in part.content:
                    src = os.path.join(self.unpackdir, 'gadget', file.source)
                    dst = os.path.join(target_dir, file.target)
                    if not file.source.endswith('/'):
                        # XXX: If this is a directory instead of a file, give
                        # a useful error message instead of stacktracing.
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        shutil.copy(src, dst)
                    else:
                        # XXX: If this is a file instead of a directory, give
                        # a useful error message instead of stacktracing.

                        # The target of a recursive directory copy is the
                        # target directory name, with or without a trailing
                        # slash necessary at least to handle the case of
                        # recursive copy into the root directory), so make
                        # sure here that it exists.
                        os.makedirs(dst, exist_ok=True)
                        target = file.target.rstrip('/')
                        for filename in os.listdir(src):
                            sub_src = os.path.join(src, filename)
                            dst = os.path.join(target_dir, target,
                                               filename)
                            if sub_src is dir:
                                shutil.copytree(sub_src, dst, symlinks=True,
                                                ignore_dangling_symlinks=True)
                            else:
                                shutil.copy(sub_src, dst)

        self._next.append(self.calculate_bootfs_size)

    def calculate_bootfs_size(self):
        volumes = self.gadget.volumes.values()
        assert len(volumes) == 1, 'For now, only one volume is allowed'
        volume = list(volumes)[0]
        self.bootfs_sizes = {}
        # At least one structure is required.
        for i, part in enumerate(volume.structures):
            partnum = 'part{}'.format(i)
            target_dir = os.path.join(self.workdir, partnum)
            if part.filesystem is FileSystemType.none:
                continue                            # pragma: nocover
            self.bootfs_sizes[partnum] = self._calculate_dirsize(target_dir)
        self._next.append(self.prepare_filesystems)

    def prepare_filesystems(self):
        self.images = os.path.join(self.workdir, '.images')
        os.makedirs(self.images)
        # The image for the boot partition.
        self.boot_images = []
        volumes = self.gadget.volumes.values()
        assert len(volumes) == 1, 'For now, only one volume is allowed'
        volume = list(volumes)[0]
        for partnum, part in enumerate(volume.structures):
            part_img = os.path.join(self.images, 'part{}.img'.format(partnum))
            self.boot_images.append(part_img)
            run('dd if=/dev/zero of={} count=0 bs={} seek=1'.format(
                part_img, part.size))
            if part.filesystem is FileSystemType.vfat:   # pragma: nobranch
                run('mkfs.vfat {}'.format(part_img))
            # XXX: Does not handle the case of partitions at the end of the
            # image.
            next_avail = part.offset + part.size
        # The image for the root partition.
        #
        # XXX: Hard-codes 4GB image size.   Hard-codes last sector for backup
        # GPT.
        avail_space = (4000000000 - next_avail - 4 * 1024) // MiB(1)
        if self.rootfs_size / MiB(1) > avail_space:   # pragma: nocover
            raise AssertionError('No room for root filesystem data')
        self.rootfs_size = avail_space
        self.root_img = os.path.join(self.images, 'root.img')
        # create empty file with holes
        with open(self.root_img,  "w"):
            pass
        os.truncate(self.root_img, avail_space * MiB(1))
        # We defer creating the root file system image because we have to
        # populate it at the same time.  See mkfs.ext4(8) for details.
        self._next.append(self.populate_filesystems)

    def populate_filesystems(self):
        volumes = self.gadget.volumes.values()
        assert len(volumes) == 1, 'For now, only one volume is allowed'
        volume = list(volumes)[0]
        for partnum, part in enumerate(volume.structures):
            part_img = self.boot_images[partnum]
            part_dir = os.path.join(self.workdir, 'part{}'.format(partnum))
            if part.filesystem is FileSystemType.none:   # pragma: nocover
                image = Image(part_img, part.size)
                offset = 0
                for file in part.content:
                    src = os.path.join(self.unpackdir, 'gadget', file.image)
                    file_size = os.path.getsize(src)
                    if file.size is not None and file.size < file_size:
                        raise AssertionError('Size {} < size of {}'
                                             .format(file.size, file.image))
                    if file.size is not None:
                        file_size = file.size
                    # XXX: We need to check for overlapping images.
                    if file.offset is not None:
                        offset = file.offset
                    # XXX: We must check offset+size vs. the target image.
                    image.copy_blob(src, bs=1, seek=offset, conv='notrunc')
                    offset += file_size

            elif part.filesystem is FileSystemType.vfat:    # pragma: nobranch
                sourcefiles = SPACE.join(
                    os.path.join(part_dir, filename)
                    for filename in os.listdir(part_dir)
                    )
                run('mcopy -s -i {} {} ::'.format(part_img, sourcefiles),
                    env=dict(MTOOLS_SKIP_CHECK='1', PATH=os.environ["PATH"]))
            elif part.filesystem is FileSystemType.ext4:   # pragma: nocover
                _mkfs_ext4(self.part_img, part_dir, part.filesystem_label)
        # The root partition needs to be ext4, which may or may not be
        # populated at creation time, depending on the version of e2fsprogs.
        _mkfs_ext4(self.root_img, self.rootfs)
        self._next.append(self.make_disk)

    def make_disk(self):
        self.disk_img = os.path.join(self.images, 'disk.img')
        part_id = 1
        # Walk through all partitions and write them to the disk image at the
        # lowest permissible offset.  We should not have any overlapping
        # partitions, the parser should have already rejected such as invalid.
        #
        # XXX: The parser should sort these partitions for us in disk order as
        # part of checking for overlaps, so we should not need to sort them
        # here.
        volumes = self.gadget.volumes.values()
        assert len(volumes) == 1, 'For now, only one volume is allowed'
        volume = list(volumes)[0]
        # XXX: This ought to be a single constructor that figures out the
        # class for us when we pass in the schema.
        if volume.schema == VolumeSchema.mbr:
            image = MBRImage(self.disk_img, 4000000000)
        else:
            image = Image(self.disk_img, 4000000000)

        structures = sorted(volume.structures, key=attrgetter('offset'))
        offset_writes = []
        part_offsets = {}
        for i, part in enumerate(structures):
            if part.name:                           # pragma: nocover
                part_offsets[part.name] = part.offset
            if part.offset_write:                   # pragma: nocover
                offset_writes.append((part.offset, part.offset_write))
            image.copy_blob(self.boot_images[i],
                            bs='1M', seek=part.offset // MiB(1),
                            count=ceil(part.size / MiB(1)),
                            conv='notrunc')
            if part.type == 'mbr':
                continue                            # pragma: nocover
            # sgdisk takes either a sector or a KiB/MiB argument; assume
            # that the offset and size are always multiples of 1MiB.
            partdef = '{}M:+{}M'.format(
                part.offset // MiB(1), part.size // MiB(1))
            part_args = {}
            part_args['new'] = partdef
            part_args['typecode'] = part.type
            # XXX: special-casing.
            if (volume.schema == VolumeSchema.mbr and
               part.filesystem_label == 'system-boot'):
                part_args['activate'] = True
            if part.name is not None:               # pragma: nobranch
                part_args['change_name'] = part.name
            image.partition(part_id, **part_args)
            part_id += 1
            next_offset = (part.offset + part.size) // MiB(1)
        # Create main snappy writable partition
        image.partition(part_id,
                        new='{}M:+{}M'.format(next_offset, self.rootfs_size),
                        typecode=('83',
                                  '0FC63DAF-8483-4772-8E79-3D69D8477DE4'))
        if volume.schema == VolumeSchema.gpt:
            image.partition(part_id, change_name='writable')
        image.copy_blob(self.root_img,
                        bs='1M', seek=next_offset, count=self.rootfs_size,
                        conv='notrunc')
        for value, dest in offset_writes:           # pragma: nobranch
            # decipher non-numeric offset_write values
            if isinstance(dest, tuple):             # pragma: nocover
                dest = part_offsets[dest[0]] + dest[1]
            # XXX: Hard-coding of 512-byte sectors.
            image.write_value_at_offset(value // 512, dest)
        self._next.append(self.finish)

    def finish(self):
        # Move the completed disk image to destination location, since the
        # temporary scratch directory is about to get removed.
        shutil.move(self.disk_img, self.output)
        self._next.append(self.close)
