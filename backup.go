package main

import (
	"archive/tar"
	"encoding/json"
	"fmt"
	"io"
	"io/ioutil"
	"os"
	"os/exec"
	"path/filepath" //
	"strings"
	"syscall"
	"time"

	"github.com/docker/docker/api/types"
	"github.com/docker/docker/api/types/container"
	"github.com/docker/go-connections/nat"
	"github.com/kennygrant/sanitize"
	"github.com/spf13/cobra"
)

// Backup is used to gather all of a container's metadata, so we can encode it
// as JSON and store it
type Backup struct {
	Name    string
	Config  *container.Config
	PortMap nat.PortMap
	Mounts  []types.MountPoint
}

var (
	optLaunch  = ""
	optTar     = false
	optAll     = false
	optStopped = false
	optVerbose = false
	optOutput  = "./backups"   // 

	paths []string
	tw    *tar.Writer

	backupCmd = &cobra.Command{
		Use:   "backup [container-id]",
		Short: "creates a backup of a container",
		RunE: func(cmd *cobra.Command, args []string) error {
			if optAll {
				return backupAll()
			}

			if len(args) < 1 {
				return fmt.Errorf("backup requires the ID of a container")
			}
			return backup(args[0])
		},
	}
)

func collectFile(path string, info os.FileInfo, err error) error {
	if err != nil {
		return err
	}

	if optVerbose {
		fmt.Println("Adding", path)
	}

	paths = append(paths, path)
	return nil
}

func collectFileTar(path string, info os.FileInfo, err error) error {
	if err != nil {
		return err
	}
	if info.Mode()&os.ModeSocket != 0 {
		// ignore sockets
		return nil
	}

	if optVerbose {
		fmt.Println("Adding", path)
	}

	th, err := tar.FileInfoHeader(info, path)
	if err != nil {
		return err
	}

	th.Name = path
	if si, ok := info.Sys().(*syscall.Stat_t); ok {
		th.Uid = int(si.Uid)
		th.Gid = int(si.Gid)
	}

	if err := tw.WriteHeader(th); err != nil {
		return err
	}

	if !info.Mode().IsRegular() {
		return nil
	}
	if info.Mode().IsDir() {
		return nil
	}

	file, err := os.Open(path)
	if err != nil {
		return err
	}

	_, err = io.Copy(tw, file)
	return err
}

// mountFolderName get subfoldername for a mount:
// use the Docker volumename if availible (named volume),
// else get the sanitized destination-path (bind mount).
func mountFolderName(m types.MountPoint) string {
	if m.Name != "" {
		return sanitize.Path(m.Name)
	}
	name := strings.TrimPrefix(m.Destination, "/")
	name = strings.Replace(name, "/", "_", -1)
	return sanitize.Path(name)
}

// copyMount copy the entire content from src to dst,
// including folderstructure, filerights and symlinks.
func copyMount(src, dst string) error {
	return filepath.Walk(src, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}

		rel, err := filepath.Rel(src, path)
		if err != nil {
			return err
		}
		target := filepath.Join(dst, rel)

		if info.IsDir() {
			return os.MkdirAll(target, info.Mode())
		}

		if info.Mode()&os.ModeSymlink != 0 {
			linkTarget, err := os.Readlink(path)
			if err != nil {
				return err
			}
			return os.Symlink(linkTarget, target)
		}

		if !info.Mode().IsRegular() {
			// sockets, devices e.d. overslaan
			return nil
		}

		if err := os.MkdirAll(filepath.Dir(target), 0755); err != nil {
			return err
		}

		in, err := os.Open(path)
		if err != nil {
			return err
		}
		defer in.Close()

		out, err := os.OpenFile(target, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, info.Mode())
		if err != nil {
			return err
		}
		defer out.Close()

		if optVerbose {
			fmt.Println("Copying", path, "->", target)
		}

		_, err = io.Copy(out, in)
		return err
	})
}

func backupTar(backupRoot, filename string, backup Backup) error {
	b, err := json.MarshalIndent(backup, "", "  ")
	if err != nil {
		return err
	}
	// fmt.Println(string(b))

	if err := os.MkdirAll(backupRoot, 0755); err != nil {
		return err
	}

	tarPath := filepath.Join(backupRoot, filename+".tar")
	tarfile, err := os.Create(tarPath)
	if err != nil {
		return err
	}
	tw = tar.NewWriter(tarfile)

	th := &tar.Header{
		Name:       "container.json",
		Size:       int64(len(b)),
		ModTime:    time.Now(),
		AccessTime: time.Now(),
		ChangeTime: time.Now(),
		Mode:       0600,
	}

	if err := tw.WriteHeader(th); err != nil {
		return err
	}
	if _, err := tw.Write(b); err != nil {
		return err
	}

	for _, m := range backup.Mounts {
		// fmt.Printf("Mount (type %s) %s -> %s\n", m.Type, m.Source, m.Destination)

		err := filepath.Walk(m.Source, collectFileTar)
		if err != nil {
			return err
		}
	}

	tw.Close()
	fmt.Println("Created backup:", tarPath)
	return nil
}

func getFullImageName(imageName string) (string, error) {
	// If the image already specifies a tag we can safely use as-is
	if strings.Contains(imageName, ":") {
		return imageName, nil
	}

	// If the used image doesn't include tag information try to find one (if it exists).
	images, err := cli.ImageList(ctx, types.ImageListOptions{})
	if err != nil {
		// Couldn't get image list, abort
		return imageName, err
	}

	for _, image := range images {
		if (!strings.Contains(imageName, image.ID)) || len(image.RepoTags) == 0 {
			// unrelated image or image entry doesn't have any tags, move on
			continue
		}

		for _, tag := range image.RepoTags {
			// use closer matching tag if it exists
			if !strings.Contains(tag, imageName) {
				continue
			}
			return tag, nil
		}
		// If none of the tags matches the base image name, return the first tag
		return image.RepoTags[0], nil
	}

	// There is no tag on the matching image, just have to go with what was provided
	return imageName, nil
}

func backup(ID string) error {
	conf, err := cli.ContainerInspect(ctx, ID)
	if err != nil {
		return err
	}
	fmt.Printf("Creating backup of %s (%s, %s)\n", conf.Name[1:], conf.Config.Image, conf.ID[:12])

	paths = []string{}

	conf.Config.Image, err = getFullImageName(conf.Config.Image)
	if err != nil {
		return err
	}

	backup := Backup{
		Name:    conf.Name,
		PortMap: conf.HostConfig.PortBindings,
		Config:  conf.Config,
		Mounts:  conf.Mounts,
	}

	filename := sanitize.Path(fmt.Sprintf("%s-%s", conf.Config.Image, ID))
	filename = strings.Replace(filename, "/", "_", -1)

	// Determine target folder: backups/<date_time>/<containername>
	containerName := strings.TrimPrefix(conf.Name, "/")
	timestamp := time.Now().Format("2006-01-02_15-04")
	backupRoot := filepath.Join(optOutput, timestamp, containerName)

	if optTar {
		return backupTar(backupRoot, filename, backup)
	}

	if err := os.MkdirAll(backupRoot, 0755); err != nil {
		return err
	}

	b, err := json.MarshalIndent(backup, "", "  ")
	if err != nil {
		return err
	}
	// fmt.Println(string(b))

	jsonPath := filepath.Join(backupRoot, filename+".backup.json")
	err = ioutil.WriteFile(jsonPath, b, 0600)
	if err != nil {
		return err
	}

	for _, m := range conf.Mounts {
		// fmt.Printf("Mount (type %s) %s -> %s\n", m.Type, m.Source, m.Destination)
		err := filepath.Walk(m.Source, collectFile)
		if err != nil {
			return err
		}

		mountDir := filepath.Join(backupRoot, mountFolderName(m))
		if err := os.MkdirAll(mountDir, 0755); err != nil {
			return err
		}
		if err := copyMount(m.Source, mountDir); err != nil {
			return err
		}
	}

	filesPath := filepath.Join(backupRoot, filename+".backup.files")
	filelist, err := os.Create(filesPath)
	if err != nil {
		return err
	}
	defer filelist.Close()

	absJSONPath, err := filepath.Abs(jsonPath)
	if err != nil {
		return err
	}

	_, err = filelist.WriteString(absJSONPath + "\n")
	if err != nil {
		return err
	}

	for _, s := range paths {
		_, err := filelist.WriteString(s + "\n")
		if err != nil {
			return err
		}
	}

	fmt.Println("Created backup:", jsonPath)

	if optLaunch != "" {
		ol := strings.Replace(optLaunch, "%tag", filename, -1)
		ol = strings.Replace(ol, "%list", filesPath, -1)

		fmt.Println("Launching external command and waiting for it to finish:")
		fmt.Println(ol)

		l := strings.Split(ol, " ")
		cmd := exec.Command(l[0], l[1:]...)
		return cmd.Run()
	}

	return nil
}

func backupAll() error {
	containers, err := cli.ContainerList(ctx, types.ContainerListOptions{
		All: optStopped,
	})
	if err != nil {
		panic(err)
	}

	for _, container := range containers {
		err := backup(container.ID)
		if err != nil {
			return err
		}
	}

	return nil
}

func init() {
	backupCmd.Flags().StringVarP(&optLaunch, "launch", "l", "", "launch external program with file-list as argument")
	backupCmd.Flags().BoolVarP(&optTar, "tar", "t", false, "create tar backups")
	backupCmd.Flags().BoolVarP(&optAll, "all", "a", false, "backup all running containers")
	backupCmd.Flags().BoolVarP(&optStopped, "stopped", "s", false, "in combination with --all: also backup stopped containers")
	backupCmd.Flags().BoolVarP(&optVerbose, "verbose", "v", false, "print detailed backup progress")
	backupCmd.Flags().StringVarP(&optOutput, "output", "o", "./backups", "root folder for volume-mount backups")   //
	RootCmd.AddCommand(backupCmd)
}
